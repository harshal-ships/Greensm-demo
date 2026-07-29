"""Amazon Nova 2 Sonic bidirectional bridge for lost-item voice calls."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from typing import Any, Optional

from agentduet import Call
from agentduet.exceptions import BufferFullError, CallClosedError
from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.config import Config
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
)
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver
from websockets.exceptions import ConnectionClosed

from audio import (
    NOVA_INPUT_SAMPLE_RATE,
    NOVA_OUTPUT_SAMPLE_RATE,
    downsample_24k_to_16k,
)
from prompts import SYSTEM_PROMPT
from tools import TOOL_SCHEMAS, LostItemTools

logger = logging.getLogger(__name__)

MODEL_ID = os.getenv("NOVA_SONIC_MODEL_ID", "amazon.nova-2-sonic-v1:0")
REGION = (
    os.getenv("NOVA_SONIC_REGION")
    or os.getenv("AWS_DEFAULT_REGION")
    or os.getenv("AWS_REGION")
    or "us-east-1"
)
VOICE_ID = os.getenv("NOVA_SONIC_VOICE_ID", "matthew")
NOVA_ENDPOINTING = os.getenv("NOVA_ENDPOINTING_SENSITIVITY", "HIGH")


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


class NovaSonicIntegration:
    """Media bridge: AgentDuet call audio ↔ Nova Sonic (+ tool dispatch)."""

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
        self._stream_broken = False
        self._terminated = False
        self._call_started_at = 0.0
        self._first_audio_logged = False
        self._last_user_at = 0.0
        self._pending_tool: dict[str, Any] | None = None
        self._tool_tasks: set[asyncio.Task] = set()
        self._tool_lock = asyncio.Lock()

    async def shutdown(self) -> None:
        """Single terminate path: tools → Pipedrive finalize → Nova teardown."""
        if self._terminated:
            return
        self._terminated = True
        logger.info("Call terminated — cleaning up Nova session")

        if self._tool_tasks:
            await asyncio.gather(*list(self._tool_tasks), return_exceptions=True)

        try:
            result = await self._tools.finalize_after_call()
            if result and result.get("ok"):
                logger.info(
                    "Post-call ticket ready ticket_id=%s ride=%s",
                    result.get("ticket_id"),
                    result.get("ride_id"),
                )
        except Exception:
            logger.exception("Post-call ticket finalize failed")

        self._is_active = False
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
        # Match driver-payment: allow teardown events while stream exists
        # (do not gate on _is_active).
        if not self._stream or self._stream_broken:
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
                        "content": (
                            "The call just connected. Deliver your opening greeting now."
                        ),
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
            "Opening Nova stream model=%s tools=lookupRecentRide,notifyDriverLostItem",
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
        await self.shutdown()

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
            "pipedrive_deferred",
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
        if not self._stream or self._stream_broken or self._terminated:
            logger.warning("Skipping tool result — Nova stream inactive")
            return

        payload = self._tool_result_payload(result)
        content = json.dumps(payload, ensure_ascii=True)
        content_name = str(uuid.uuid4())

        async with self._tool_lock:
            if not self._stream or self._stream_broken or self._terminated:
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
                                "I hit a technical issue. "
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
                            logger.debug(
                                "clear_send_audio_buffer failed", exc_info=True
                            )
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
                            logger.info("[ASSISTANT] %s", text_content[:160])

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
