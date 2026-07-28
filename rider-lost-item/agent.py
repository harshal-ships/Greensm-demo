"""
GreenSM rider lost-item voice agent.

A rider calls in → Nova 2 Sonic gathers details → looks up the ride →
creates a Pipedrive ticket → WhatsApps the driver via AgentDuet.
Driver WhatsApp replies are logged back onto the Pipedrive deal.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from agentduet import (
    Call,
    CallAudioConfig,
    InboundCallMode,
    IncomingCallNotification,
    IncomingMessage,
    SendWAMessage,
    SessionManager,
    SessionManagerConfig,
    TriggerConditionsBuilder,
    new_session_id,
)
from agentduet.events import Network
from agentduet.exceptions import BufferFullError, CallClosedError, CallNotFoundError
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

from pipedrive_client import PipedriveClient
from tools import TOOL_SCHEMAS, LostItemTools, TicketRegistry

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logging.getLogger("agentduet").setLevel(logging.DEBUG)
logging.getLogger("agentduet.voice_session").setLevel(logging.DEBUG)

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
OPENING_GREETING = os.getenv(
    "NOVA_OPENING_GREETING",
    "Hello, you've reached GreenSM lost item support. I can help you report something left in a recent ride.",
)
WA_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v23.0")

SYSTEM_PROMPT = f"""You are GreenSM's lost-item support agent on a live phone call with a rider.
Speak warmly, clearly, and briefly — one or two short sentences at a time.
When the call connects, speak this opening line first: "{OPENING_GREETING}"

Your job:
1. Collect what they lost (item description).
2. Identify the ride: ask for approximate time, pickup, dropoff, and/or ride id.
3. Call lookupRecentRide to find matching rides. Confirm the correct ride with the rider.
4. Once the rider confirms, call fileLostItemReport with ride_id and item_description.
   Do NOT ask for a callback phone number.
5. After the tool returns, tell the rider the outcome using agent_speak_summary.
   If WhatsApp failed, you MUST say so clearly — never pretend the driver was messaged.
   If WhatsApp succeeded, that is enough — thank them and close politely.

Rules:
- Do not invent rides or drivers. Only use lookupRecentRide results.
- Do not file a report until the rider confirms the ride and details.
- Keep replies short for phone audio.
- If multiple rides match, ask which one.
- Ride ids are like GRN-88421. If the rider spells letters, treat G-R-N as GRN.
- Never ask for a callback number.
"""


def downsample_24k_to_16k(pcm_24k: bytes) -> bytes:
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


def _extract_wa_text(payload: dict) -> str:
    """Best-effort text body from a WhatsApp webhook payload."""
    try:
        entry = payload.get("entry") or []
        for e in entry:
            for change in e.get("changes") or []:
                value = change.get("value") or {}
                for msg in value.get("messages") or []:
                    if msg.get("type") == "text":
                        return (msg.get("text") or {}).get("body") or ""
                    if "text" in msg:
                        return str(msg["text"])
    except Exception:
        pass
    # Fallback: stringify small payload
    text = payload.get("text") or payload.get("body") or ""
    if isinstance(text, dict):
        return text.get("body") or json.dumps(text)
    return str(text) if text else json.dumps(payload)[:500]


async def attach_inbound_call(
    sm: SessionManager, noti: IncomingCallNotification
) -> Call:
    session = await sm.open_session(new_session_id(), noti.subscriber)
    try:
        return await session.process_call(noti)
    except CallNotFoundError:
        session = await sm.open_session(new_session_id(), noti.subscriber)
        return await session.process_call(noti)


class WhatsAppSender:
    """Outbound WhatsApp via AgentDuet — matches the 1.0.0b9 docs pattern.

    Docs (Reply to a WhatsApp message):
      session = await sm.open_session(new_session_id(), msg.subscriber)  # WA business id
      await session.send_message(SendWAMessage(..., data={..., "to": msg.participant.value}))

    Critical: ``subscriber`` must be the **WhatsApp business identity**, not the
    TELCO number from an inbound voice call. Using the voice-call subscriber is
    what produced ``inbox.NotFound`` / "Whatsapp number not found."
    """

    def __init__(self, sm: SessionManager, default_subscriber: str | None = None):
        self._sm = sm
        # Prefer explicit WA subscriber; never fall back to a TELCO call subscriber.
        self._wa_subscriber = (
            default_subscriber
            or os.getenv("AGENTDUET_WA_SUBSCRIBER", "").strip()
        )

    def remember_wa_subscriber(self, subscriber: str) -> None:
        """Only call this from @sm.on_incoming_message (WA channel), not voice calls."""
        if subscriber:
            self._wa_subscriber = subscriber
            logger.info("WhatsApp business subscriber set to %s", subscriber)

    async def send_text(self, to_whatsapp: str, body: str) -> dict[str, Any]:
        subscriber = self._wa_subscriber
        if not subscriber:
            return {
                "success": False,
                "error_code": "NO_WA_SUBSCRIBER",
                "error_message": (
                    "Set AGENTDUET_WA_SUBSCRIBER to your WhatsApp business number "
                    "(the subscriber on WA messages), not the phone-call TELCO number. "
                    "See agentduet 1.0.0b9: open_session(session_id, msg.subscriber)."
                ),
            }

        if not (to_whatsapp or "").strip():
            return {
                "success": False,
                "error_code": "NO_RECIPIENT",
                "error_message": "Driver WhatsApp number is empty",
            }

        # Docs use participant.value (E.164 with '+'). Some providers want digits only —
        # try with '+', then without.
        raw = to_whatsapp.strip()
        candidates = []
        with_plus = raw if raw.startswith("+") else f"+{raw.lstrip('+')}"
        digits_only = re.sub(r"\D", "", raw)
        for candidate in (with_plus, digits_only):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        last_error: dict[str, Any] = {
            "success": False,
            "error_code": "SEND_FAILED",
            "error_message": "WhatsApp send failed",
        }

        for to in candidates:
            try:
                session = await self._sm.open_session(new_session_id(), subscriber)
                result = await session.send_message(
                    SendWAMessage(
                        api_version=WA_API_VERSION,
                        data={
                            "messaging_product": "whatsapp",
                            "type": "text",
                            "to": to,
                            "text": {"body": body},
                        },
                    )
                )
            except Exception as exc:
                logger.exception("WhatsApp send raised for to=%s", to)
                last_error = {
                    "success": False,
                    "error_code": "EXCEPTION",
                    "error_message": str(exc),
                }
                continue

            if result.success:
                logger.info("WhatsApp sent to %s via subscriber %s", to, subscriber)
                return {"success": True}

            err_content = result.error_content
            if not isinstance(err_content, str):
                err_content = json.dumps(err_content, default=str)
            last_error = {
                "success": False,
                "error_code": str(result.error_code),
                "error_message": err_content or str(result.error_code),
            }
            logger.warning(
                "WhatsApp attempt failed to=%s subscriber=%s: %s %s",
                to,
                subscriber,
                result.error_code,
                err_content,
            )

        logger.error(
            "WhatsApp failed for all formats to %s (subscriber=%s): %s",
            to_whatsapp,
            subscriber,
            last_error,
        )
        return last_error


class NovaSonicIntegration:
    def __init__(
        self,
        call: Call,
        client: BedrockRuntimeClient,
        tools: LostItemTools,
    ):
        self._call = call
        self._client = client
        self._tools = tools
        self._stream = None

        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        self._send_to_nova_task: Optional[asyncio.Task] = None
        self._recv_from_nova_task: Optional[asyncio.Task] = None
        self._is_active = False
        self._call_started_at = 0.0
        self._first_audio_logged = False
        self._last_user_at = 0.0
        self._pending_tool: dict[str, Any] | None = None
        self._tool_tasks: set[asyncio.Task] = set()
        self._tool_lock = asyncio.Lock()
        self._stream_broken = False

    async def _on_terminated(self) -> None:
        if not self._is_active and self._stream is None:
            return
        logger.info("Call terminated — cleaning up Nova session")
        self._is_active = False
        for task in (self._send_to_nova_task, self._recv_from_nova_task):
            if task and not task.done():
                task.cancel()
        for task in list(self._tool_tasks):
            if not task.done():
                task.cancel()
        tasks = [
            t
            for t in (
                self._send_to_nova_task,
                self._recv_from_nova_task,
                *list(self._tool_tasks),
            )
            if t
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.end_session()
        except Exception:
            pass
        logger.info("Nova session closed")

    async def send_event(self, payload: dict | str) -> None:
        if not self._stream or not self._is_active or self._stream_broken:
            raise RuntimeError("Nova stream is not active")
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
                        "maxTokens": 1024,
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
                        "toolUseOutputConfiguration": {
                            "mediaType": "application/json"
                        },
                        "toolConfiguration": {"tools": TOOL_SCHEMAS},
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
                        "content": SYSTEM_PROMPT,
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
            "Opening Nova stream model=%s tools=lookupRecentRide,fileLostItemReport",
            MODEL_ID,
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
                if not self._is_active or self._stream_broken:
                    break
                if not audio_chunk:
                    continue
                try:
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
                except RuntimeError:
                    break
                except CallClosedError:
                    break
                except Exception:
                    if self._is_active and not self._stream_broken:
                        logger.exception("Dropping audio chunk after send error")
                    else:
                        break
        except ConnectionClosed:
            pass
        except CallClosedError:
            logger.debug("Caller audio stream closed with call")
        except asyncio.CancelledError:
            pass
        except Exception:
            if self._is_active:
                logger.exception("Error streaming caller audio to Nova")
        finally:
            logger.debug("Stream to Nova completed")

    def _tool_result_payload(self, result: dict) -> dict[str, Any]:
        """Build a JSON-serializable tool payload.

        Nova Sonic rejects plain-string tool results with
        ``Tool Response parsing error`` — content must be valid JSON.
        """
        payload: dict[str, Any] = {
            "ok": bool(result.get("ok", True)),
            "message": (
                result.get("agent_speak_summary")
                or result.get("message")
                or result.get("error")
                or "Done."
            ),
        }
        if "count" in result:
            payload["count"] = result["count"]
        if "rides" in result:
            # Keep compact — avoid huge nested blobs.
            compact = []
            for r in (result.get("rides") or [])[:5]:
                compact.append(
                    {
                        "ride_id": r.get("ride_id"),
                        "pickup": r.get("pickup"),
                        "dropoff": r.get("dropoff"),
                        "driver_name": r.get("driver_name"),
                        "summary": r.get("summary"),
                    }
                )
            payload["rides"] = compact
        for key in (
            "pipedrive_ok",
            "whatsapp_ok",
            "ticket_id",
            "driver_name",
            "ride_id",
            "error",
        ):
            if key in result and result[key] is not None:
                payload[key] = result[key]
        if result.get("whatsapp_error"):
            err = result["whatsapp_error"]
            payload["whatsapp_error"] = err if isinstance(err, str) else str(err)
        return payload

    async def _send_tool_result(self, tool_use_id: str, result: dict) -> None:
        if not self._is_active or self._stream_broken or not self._stream:
            logger.warning("Skipping tool result — Nova stream inactive")
            return

        payload = self._tool_result_payload(result)
        content = json.dumps(payload, ensure_ascii=True)
        content_name = str(uuid.uuid4())

        async with self._tool_lock:
            if not self._is_active or self._stream_broken or not self._stream:
                return
            try:
                await self.send_event(
                    {
                        "event": {
                            "contentStart": {
                                "promptName": self.prompt_name,
                                "contentName": content_name,
                                "interactive": False,
                                "type": "TOOL",
                                "role": "TOOL",
                                "toolResultInputConfiguration": {
                                    "toolUseId": tool_use_id,
                                    "type": "TEXT",
                                    "textInputConfiguration": {
                                        "mediaType": "text/plain"
                                    },
                                },
                            }
                        }
                    }
                )
                await self.send_event(
                    {
                        "event": {
                            "toolResult": {
                                "promptName": self.prompt_name,
                                "contentName": content_name,
                                "content": content,
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
            except Exception:
                logger.exception("Failed to send tool result to Nova")
                self._stream_broken = True

    def _schedule_tool(
        self, tool_name: str, tool_content: dict, tool_use_id: str
    ) -> None:
        async def _run() -> None:
            try:
                if not tool_name or not tool_use_id:
                    logger.error("Invalid tool use (missing name/id)")
                    return
                result = await self._tools.execute(tool_name, tool_content or {})
                await self._send_tool_result(tool_use_id, result)
                logger.info("Tool %s completed: %s", tool_name, result)
            except Exception:
                logger.exception("Tool %s failed", tool_name)
                try:
                    await self._send_tool_result(
                        tool_use_id,
                        {
                            "ok": False,
                            "error": f"Tool {tool_name} failed",
                            "agent_speak_summary": (
                                "I hit a technical issue filing that. "
                                "Please say the details again and I will retry."
                            ),
                        },
                    )
                except Exception:
                    logger.exception("Could not send tool error result")

        task = asyncio.create_task(_run())
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def receive_audio_from_nova(self) -> None:
        try:
            logger.info("Receiving audio from Nova")
            while self._is_active:
                if self._stream_broken:
                    break
                if not self._stream:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    output = await self._stream.await_output()
                    result = await output[1].receive()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    name = type(exc).__name__
                    msg = str(exc)
                    if "ValidationException" in name or "ValidationException" in msg:
                        logger.warning(
                            "Nova ValidationException: %s — marking stream broken",
                            msg,
                        )
                        self._stream_broken = True
                        self._is_active = False
                        break
                    if self._is_active:
                        logger.exception("Nova receive failed")
                    break

                if result is None or not getattr(result, "value", None):
                    continue
                if not result.value.bytes_:
                    continue

                try:
                    json_data = json.loads(result.value.bytes_.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning("Ignoring non-JSON Nova chunk")
                    continue

                event = json_data.get("event", {})

                if "textOutput" in event:
                    text_content = event["textOutput"].get("content") or ""
                    role = event["textOutput"].get("role", "")
                    if _is_interruption_marker(text_content):
                        try:
                            await self._call.clear_send_audio_buffer()
                        except CallClosedError:
                            break
                        except Exception:
                            logger.debug("clear_send_audio_buffer failed", exc_info=True)
                    elif role == "USER":
                        self._last_user_at = time.monotonic()
                        logger.info("[USER] %s", text_content[:160])
                    elif role == "ASSISTANT":
                        if self._last_user_at:
                            logger.info(
                                "[ASSISTANT] (%.2fs) %s",
                                time.monotonic() - self._last_user_at,
                                text_content[:160],
                            )
                        else:
                            logger.debug("Nova: %s", text_content[:160])

                elif "audioOutput" in event:
                    if not self._first_audio_logged and self._call_started_at:
                        logger.info(
                            "First Nova audio after %.2fs",
                            time.monotonic() - self._call_started_at,
                        )
                        self._first_audio_logged = True
                    try:
                        audio_bytes = base64.b64decode(
                            event["audioOutput"]["content"]
                        )
                        await self._call.send_audio(audio_bytes)
                    except BufferFullError:
                        logger.warning("Send buffer full — dropping chunk")
                    except CallClosedError:
                        break
                    except Exception:
                        logger.debug("send_audio failed", exc_info=True)

                elif "toolUse" in event:
                    tool_use = event["toolUse"] or {}
                    self._pending_tool = {
                        "content": tool_use,
                        "name": tool_use.get("toolName"),
                        "id": tool_use.get("toolUseId"),
                    }
                    logger.info(
                        "Tool use: %s id=%s",
                        self._pending_tool["name"],
                        self._pending_tool["id"],
                    )

                elif (
                    "contentEnd" in event
                    and event["contentEnd"].get("type") == "TOOL"
                    and self._pending_tool
                ):
                    pending = self._pending_tool
                    self._pending_tool = None
                    self._schedule_tool(
                        pending.get("name") or "",
                        pending.get("content") or {},
                        pending.get("id") or "",
                    )

        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception:
            if self._is_active:
                logger.exception("Error receiving audio from Nova")
        finally:
            logger.debug("Stream from Nova completed")


async def handle_voice_call(
    sm: SessionManager,
    noti: IncomingCallNotification,
    bedrock_client: BedrockRuntimeClient,
    tools: LostItemTools,
    wa_sender: WhatsAppSender,
) -> None:
    channel = "WhatsApp call" if noti.network == Network.WA else "Phone call"
    logger.info(
        "%s %s from %s", channel, noti.call_id, noti.participant.value
    )
    # Do NOT set WA subscriber from a voice call (usually TELCO → inbox.NotFound).
    # WA subscriber comes from AGENTDUET_WA_SUBSCRIBER or inbound WhatsApp messages.

    try:
        call = await attach_inbound_call(sm, noti)
    except Exception:
        logger.exception("Failed to attach call %s", noti.call_id)
        return

    nova = NovaSonicIntegration(call, bedrock_client, tools)
    loop = asyncio.get_running_loop()

    @call.on_hangup
    def on_hangup(_payload: object = None) -> None:
        # Hangup handlers run in a worker thread — schedule on the event loop.
        def _schedule() -> None:
            asyncio.create_task(nova._on_terminated())

        try:
            loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            logger.debug("Event loop closed during hangup cleanup")

    try:
        answer_task = asyncio.create_task(call.answer())
        prepare_task = asyncio.create_task(nova.prepare())

        answered = False
        try:
            answered = bool(await answer_task)
        except Exception:
            logger.exception("call.answer() raised for %s", call.id)
            answered = False

        if not answered:
            logger.error("Failed to answer call %s", getattr(call, "id", "?"))
            if not prepare_task.done():
                prepare_task.cancel()
                await asyncio.gather(prepare_task, return_exceptions=True)
            await nova.cancel()
            try:
                await call.close()
            except Exception:
                pass
            return

        try:
            await prepare_task
        except Exception:
            logger.exception("Nova prepare failed for call %s", call.id)
            await nova.cancel()
            try:
                await call.close()
            except Exception:
                pass
            return

        await nova.run_bridge()
    except Exception:
        logger.exception("Error in lost-item voice integration")
        try:
            await nova.cancel()
        except Exception:
            pass
        try:
            await call.close()
        except Exception:
            pass
    finally:
        # Ensure cleanup even if hangup handler raced with shutdown
        try:
            await nova._on_terminated()
        except Exception:
            pass


async def handle_incoming_message(
    msg: IncomingMessage,
    pipedrive: PipedriveClient,
    registry: TicketRegistry,
    wa_sender: WhatsAppSender | None = None,
) -> None:
    """Log driver WhatsApp replies onto the matching Pipedrive deal."""
    # Docs: msg.subscriber is the WhatsApp business identity for this channel.
    if wa_sender is not None:
        wa_sender.remember_wa_subscriber(msg.subscriber)

    phone = msg.participant.value
    meta = registry.find(phone)
    text = _extract_wa_text(msg.payload if isinstance(msg.payload, dict) else {})
    logger.info("Incoming WhatsApp from %s: %s", phone, text[:200])

    if not meta:
        logger.info("No open lost-item ticket mapped to %s — ignoring", phone)
        return

    note = (
        f"Driver WhatsApp reply (from {phone}) for ride {meta['ride_id']}: {text}"
    )
    try:
        pipedrive.add_note_to_deal(meta["deal_id"], note)
        logger.info("Logged driver reply onto deal %s", meta["deal_id"])
    except Exception:
        logger.exception("Failed to log driver reply to Pipedrive")


async def main() -> None:
    api_key = os.getenv("AGENTDUET_API_KEY")
    connector_uuid = os.getenv("AGENTDUET_CONNECTOR_UUID")
    if not api_key or not connector_uuid:
        raise RuntimeError("Set AGENTDUET_API_KEY and AGENTDUET_CONNECTOR_UUID")

    bedrock_client = create_bedrock_client()
    pipedrive = PipedriveClient()
    registry = TicketRegistry()

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
            wa_sender = WhatsAppSender(sm)
            if not wa_sender._wa_subscriber:
                logger.warning(
                    "AGENTDUET_WA_SUBSCRIBER is not set — WhatsApp sends will fail "
                    "until you set the WhatsApp business subscriber"
                )
            tools = LostItemTools(
                pipedrive=pipedrive,
                registry=registry,
                send_whatsapp=wa_sender.send_text,
            )

            logger.info(
                "GreenSM lost-item agent connected (model=%s pipedrive_mock=%s wa_subscriber=%s)",
                MODEL_ID,
                pipedrive.mock_mode,
                bool(wa_sender._wa_subscriber),
            )

            try:
                await sm.setup_trigger_conditions(
                    TriggerConditionsBuilder()
                    .inbound_call(InboundCallMode.ALL)
                    .inbound_message(True)
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
                        sm, noti, bedrock_client, tools, wa_sender
                    )
                except Exception:
                    logger.exception("Unhandled error on call %s", noti.call_id)
                finally:
                    inflight_calls.discard(noti.call_id)

            @sm.on_incoming_message
            async def on_message(msg: IncomingMessage) -> None:
                try:
                    await handle_incoming_message(
                        msg, pipedrive, registry, wa_sender
                    )
                except Exception:
                    logger.exception("Unhandled error on message %s", msg.id)

            await sm.run_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    asyncio.run(main())
