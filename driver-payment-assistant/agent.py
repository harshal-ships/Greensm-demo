"""
GreenSM driver payment voice agent.

Handles phone (TELCO) and WhatsApp (WA) inbound voice calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import time
import uuid
from pathlib import Path
from typing import Optional

from agentduet import (
    Call,
    CallAudioConfig,
    InboundCallMode,
    IncomingCallNotification,
    SessionManager,
    SessionManagerConfig,
    TriggerConditionsBuilder,
    new_session_id,
)
from agentduet.events import Network
from agentduet.exceptions import BufferFullError, CallNotFoundError
from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.config import Config
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
)
from dotenv import load_dotenv
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver
from websockets.exceptions import ConnectionClosed

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# Quiet AgentDuet internals (voice credit/flow spam); keep [USER] / [ASSISTANT] from this app.
logging.getLogger("agentduet").setLevel(logging.WARNING)

MODEL_ID = os.getenv("NOVA_SONIC_MODEL_ID", "amazon.nova-2-sonic-v1:0")
REGION = (
    os.getenv("NOVA_SONIC_REGION")
    or os.getenv("AWS_DEFAULT_REGION")
    or os.getenv("AWS_REGION")
    or "us-east-1"
)
VOICE_ID = os.getenv("NOVA_SONIC_VOICE_ID", "matthew")
AGENTDUET_SAMPLE_RATE = 24000
NOVA_INPUT_SAMPLE_RATE = 16000
NOVA_OUTPUT_SAMPLE_RATE = 24000
NOVA_ENDPOINTING = os.getenv("NOVA_ENDPOINTING_SENSITIVITY", "HIGH")
POLICY_PATH = Path(__file__).resolve().parent / "policy" / "greensm_driver_payment_policy.md"
OPENING_GREETING = os.getenv(
    "NOVA_OPENING_GREETING",
    "Hello, this is GreenSM's driver payment assistant. How can I help you today?",
)


def load_policy() -> str:
    if not POLICY_PATH.is_file():
        raise FileNotFoundError(f"Payment policy not found: {POLICY_PATH}")
    return POLICY_PATH.read_text(encoding="utf-8")


def build_system_prompt(policy: str) -> str:
    return (
        "You are GreenSM's driver payment assistant on a live phone call. "
        "Speak warmly, professionally, and briefly—like a helpful human agent. "
        f'When the call connects, speak this opening line first: "{OPENING_GREETING}" '
        "Then listen and reply in one or two short sentences. "
        "Respond as soon as the caller stops speaking.\n\n"
        "Rules:\n"
        "1. Answer ONLY using the GreenSM Driver Payment Policy attached below. "
        "Never invent payout rules, fees, dates, or thresholds.\n"
        "2. Prefer concrete policy facts (days, fees, dollar amounts) when the driver asks about pay.\n"
        "3. Stay within about 3–5 short sentences total per reply.\n"
        "4. If the question is outside this payment policy, say you can only help with "
        "GreenSM payment policy and offer escalation to a Driver Pay Specialist or "
        "Wallet → Help → Pay & wallet in the Driver app.\n"
        "5. Do not give tax advice beyond where to find tax documents in the app.\n\n"
        "--- BEGIN GREENSM DRIVER PAYMENT POLICY ---\n"
        f"{policy}\n"
        "--- END GREENSM DRIVER PAYMENT POLICY ---"
    )


def downsample_24k_to_16k(pcm_24k: bytes) -> bytes:
    """Resample AgentDuet 24 kHz PCM to Nova Sonic 16 kHz input."""
    if len(pcm_24k) < 2:
        return pcm_24k
    samples = struct.unpack(f"<{len(pcm_24k) // 2}h", pcm_24k)
    n_out = int(len(samples) * NOVA_INPUT_SAMPLE_RATE / AGENTDUET_SAMPLE_RATE)
    ratio = AGENTDUET_SAMPLE_RATE / NOVA_INPUT_SAMPLE_RATE
    out: list[int] = []
    for i in range(n_out):
        src = i * ratio
        idx = int(src)
        frac = src - idx
        if idx + 1 < len(samples):
            val = int(samples[idx] * (1 - frac) + samples[idx + 1] * frac)
        else:
            val = samples[min(idx, len(samples) - 1)]
        out.append(max(-32768, min(32767, val)))
    return struct.pack(f"<{len(out)}h", *out)


def create_bedrock_client() -> BedrockRuntimeClient:
    config = Config(
        endpoint_uri=f"https://bedrock-runtime.{REGION}.amazonaws.com",
        region=REGION,
        aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
    )
    return BedrockRuntimeClient(config=config)


def _is_interruption_marker(text: str) -> bool:
    return '{ "interrupted" : true }' in text or (
        '"interrupted"' in text and "true" in text
    )


async def attach_inbound_call(sm: SessionManager, noti: IncomingCallNotification) -> Call:
    session = await sm.open_session(new_session_id(), noti.subscriber)
    try:
        return await session.process_call(noti)
    except CallNotFoundError:
        session = await sm.open_session(new_session_id(), noti.subscriber)
        return await session.process_call(noti)


class NovaSonicIntegration:
    """Bidirectional Nova Sonic bridge for TELCO and WhatsApp voice calls."""

    def __init__(self, call: Call, client: BedrockRuntimeClient, system_prompt: str):
        self._call = call
        self._client = client
        self._system_prompt = system_prompt
        self._stream = None

        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        self._send_to_nova_task: Optional[asyncio.Task] = None
        self._recv_from_nova_task: Optional[asyncio.Task] = None
        self._is_active = False
        self._closing = False
        self._call_started_at = 0.0
        self._first_audio_logged = False
        self._last_user_at = 0.0

    async def _on_terminated(self) -> None:
        if not self._is_active:
            return

        logger.info("Call terminated — cleaning up Nova session")
        self._is_active = False
        self._closing = True

        for task in (self._send_to_nova_task, self._recv_from_nova_task):
            if task and not task.done():
                task.cancel()

        tasks = [t for t in (self._send_to_nova_task, self._recv_from_nova_task) if t]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await self.end_session()
        except Exception:
            pass

        logger.info("Nova session closed")

    async def send_event(self, payload: dict | str) -> None:
        raw = json.dumps(payload) if isinstance(payload, dict) else payload
        event = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=raw.encode("utf-8"))
        )
        await self._stream.input_stream.send(event)

    async def _send_session_setup(self) -> None:
        session_start: dict = {
            "event": {
                "sessionStart": {
                    "inferenceConfiguration": {
                        "maxTokens": 512,
                        "topP": 0.9,
                        "temperature": 0.7,
                    },
                }
            }
        }
        if "nova-2-sonic" in MODEL_ID.lower():
            session_start["event"]["sessionStart"]["turnDetectionConfiguration"] = {
                "endpointingSensitivity": NOVA_ENDPOINTING,
            }

        await self.send_event(session_start)
        await self.send_event(
            {
                "event": {
                    "promptStart": {
                        "promptName": self.prompt_name,
                        "textOutputConfiguration": {"mediaType": "text/plain"},
                        "audioOutputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": NOVA_OUTPUT_SAMPLE_RATE,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "voiceId": VOICE_ID,
                            "encoding": "base64",
                            "audioType": "SPEECH",
                        },
                    }
                }
            }
        )
        await self.send_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                        "type": "TEXT",
                        "interactive": True,
                        "role": "SYSTEM",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    }
                }
            }
        )
        await self.send_event(
            {
                "event": {
                    "textInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                        "content": self._system_prompt,
                    }
                }
            }
        )
        await self.send_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": self.content_name,
                    }
                }
            }
        )
        await self.send_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                        "type": "AUDIO",
                        "interactive": True,
                        "role": "USER",
                        "audioInputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": NOVA_INPUT_SAMPLE_RATE,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "audioType": "SPEECH",
                            "encoding": "base64",
                        },
                    }
                }
            }
        )

    async def _trigger_immediate_greeting(self) -> None:
        content_name = str(uuid.uuid4())
        await self.send_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": content_name,
                        "type": "TEXT",
                        "interactive": True,
                        "role": "USER",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    }
                }
            }
        )
        await self.send_event(
            {
                "event": {
                    "textInput": {
                        "promptName": self.prompt_name,
                        "contentName": content_name,
                        "content": "The call just connected. Deliver your opening greeting now.",
                    }
                }
            }
        )
        await self.send_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": content_name,
                    }
                }
            }
        )

    async def prepare(self) -> None:
        self._call_started_at = time.monotonic()
        logger.info(
            "Opening Nova stream model=%s region=%s endpointing=%s in=%dkHz out=%dkHz",
            MODEL_ID,
            REGION,
            NOVA_ENDPOINTING,
            NOVA_INPUT_SAMPLE_RATE // 1000,
            NOVA_OUTPUT_SAMPLE_RATE // 1000,
        )
        self._stream = await self._client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=MODEL_ID)
        )
        self._is_active = True
        self._recv_from_nova_task = asyncio.create_task(self.receive_audio_from_nova())
        await self._send_session_setup()
        await self._trigger_immediate_greeting()
        logger.info("Nova prepared in %.2fs", time.monotonic() - self._call_started_at)

    async def run_bridge(self) -> None:
        self._send_to_nova_task = asyncio.create_task(self.stream_to_nova())
        await asyncio.gather(self._send_to_nova_task, self._recv_from_nova_task)

    async def cancel(self) -> None:
        self._is_active = False
        self._closing = True
        for task in (self._send_to_nova_task, self._recv_from_nova_task):
            if task and not task.done():
                task.cancel()
        await self.end_session()

    async def end_session(self) -> None:
        if not self._stream:
            return

        try:
            await self.send_event(
                {
                    "event": {
                        "contentEnd": {
                            "promptName": self.prompt_name,
                            "contentName": self.audio_content_name,
                        }
                    }
                }
            )
            await self.send_event(
                {"event": {"promptEnd": {"promptName": self.prompt_name}}}
            )
            await self.send_event({"event": {"sessionEnd": {}}})
        except Exception:
            pass
        finally:
            if self._stream:
                try:
                    await self._stream.input_stream.close()
                except Exception:
                    pass
                self._stream = None

    async def stream_to_nova(self) -> None:
        try:
            async for audio_chunk in self._call.caller.audio_stream():
                if not self._is_active:
                    break

                pcm_16k = downsample_24k_to_16k(audio_chunk)
                blob = base64.b64encode(pcm_16k)
                await self.send_event(
                    {
                        "event": {
                            "audioInput": {
                                "promptName": self.prompt_name,
                                "contentName": self.audio_content_name,
                                "content": blob.decode("utf-8"),
                            }
                        }
                    }
                )
        except ConnectionClosed:
            pass
        except Exception:
            logger.exception("Error streaming caller audio to Nova")
            raise
        finally:
            logger.debug("Stream to Nova completed")

    async def receive_audio_from_nova(self) -> None:
        try:
            logger.info("Receiving audio from Nova")
            while self._is_active:
                if not self._stream:
                    await asyncio.sleep(0.1)
                    continue

                output = await self._stream.await_output()
                result = await output[1].receive()

                if not result.value or not result.value.bytes_:
                    continue

                json_data = json.loads(result.value.bytes_.decode("utf-8"))
                event = json_data.get("event", {})

                if "textOutput" in event:
                    text_content = event["textOutput"]["content"]
                    role = event["textOutput"].get("role", "")
                    if _is_interruption_marker(text_content):
                        await self._call.clear_send_audio_buffer()
                    elif role == "USER":
                        self._last_user_at = time.monotonic()
                        logger.info("[USER] %s", text_content[:120])
                    elif role == "ASSISTANT":
                        if self._last_user_at:
                            logger.info(
                                "[ASSISTANT] (%.2fs after user) %s",
                                time.monotonic() - self._last_user_at,
                                text_content[:120],
                            )
                        else:
                            logger.info("[ASSISTANT] %s", text_content[:120])

                elif "audioOutput" in event:
                    if not self._first_audio_logged and self._call_started_at:
                        elapsed = time.monotonic() - self._call_started_at
                        logger.info("First Nova audio to caller after %.2fs", elapsed)
                        self._first_audio_logged = True
                    audio_bytes = base64.b64decode(event["audioOutput"]["content"])
                    try:
                        await self._call.send_audio(audio_bytes)
                    except BufferFullError:
                        logger.warning(
                            "AgentDuet send buffer full — dropping audio chunk"
                        )

        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception:
            if self._is_active:
                logger.exception("Error receiving audio from Nova")
            raise
        finally:
            logger.debug("Stream from Nova completed")


async def handle_voice_call(
    sm: SessionManager,
    noti: IncomingCallNotification,
    bedrock_client: BedrockRuntimeClient,
    system_prompt: str,
) -> None:
    channel_label = "WhatsApp call" if noti.network == Network.WA else "Phone call"
    logger.info(
        "%s %s from %s (network=%s)",
        channel_label,
        noti.call_id,
        noti.participant.value,
        noti.network,
    )

    try:
        call = await attach_inbound_call(sm, noti)
    except Exception:
        logger.exception("Failed to attach call %s", noti.call_id)
        return

    nova = NovaSonicIntegration(call, bedrock_client, system_prompt)
    loop = asyncio.get_running_loop()

    @call.on_hangup
    def on_hangup(_payload: object = None) -> None:
        # Sync hangup handlers run in a worker thread — schedule on the main loop.
        asyncio.run_coroutine_threadsafe(nova._on_terminated(), loop)

    try:
        answer_task = asyncio.create_task(call.answer())
        prepare_task = asyncio.create_task(nova.prepare())

        if not await answer_task:
            logger.error("Failed to answer call %s", call.id)
            await nova.cancel()
            if not prepare_task.done():
                prepare_task.cancel()
                await asyncio.gather(prepare_task, return_exceptions=True)
            return

        await prepare_task
        await nova.run_bridge()
    except Exception:
        logger.exception("Error in Nova Sonic voice integration")
        try:
            await call.close()
        except Exception:
            pass


async def main() -> None:
    policy = load_policy()
    system_prompt = build_system_prompt(policy)
    logger.info("Loaded GreenSM payment policy (%d chars)", len(policy))

    bedrock_client = create_bedrock_client()

    api_key = os.getenv("AGENTDUET_API_KEY")
    connector_uuid = os.getenv("AGENTDUET_CONNECTOR_UUID")
    if not api_key or not connector_uuid:
        raise RuntimeError("Set AGENTDUET_API_KEY and AGENTDUET_CONNECTOR_UUID")

    config = SessionManagerConfig.create(
        api_key=api_key,
        connector_uuid=connector_uuid,
        call_audio=CallAudioConfig(
            sample_rate=AGENTDUET_SAMPLE_RATE, buffer_size=1024 * 1024
        ),
    )

    inflight_calls: set[str] = set()

    try:
        async with SessionManager(config) as sm:
            logger.info(
                "GreenSM agent connected (model=%s region=%s). Waiting for calls...",
                MODEL_ID,
                REGION,
            )

            try:
                await sm.setup_trigger_conditions(
                    TriggerConditionsBuilder()
                    .inbound_call(InboundCallMode.ALL)
                    .build()
                )
            except Exception as exc:
                logger.warning("Trigger setup failed (%s); continuing", exc)

            @sm.on_incoming_call
            async def on_call(noti: IncomingCallNotification) -> None:
                if noti.call_id in inflight_calls:
                    return
                inflight_calls.add(noti.call_id)
                try:
                    await handle_voice_call(
                        sm, noti, bedrock_client, system_prompt
                    )
                except Exception:
                    logger.exception("Unhandled error on call %s", noti.call_id)
                finally:
                    inflight_calls.discard(noti.call_id)

            await sm.run_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    asyncio.run(main())
