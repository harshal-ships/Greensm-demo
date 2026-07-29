"""Outbound WhatsApp via AgentDuet Session + SendWAMessage (1.0.0b9).

Docs pattern:
  session = await sm.open_session(new_session_id(), msg.subscriber)
  await session.send_message(SendWAMessage(..., data={..., \"to\": ...}))

``subscriber`` must be the WhatsApp business inbox identity — never the TELCO
voice-call subscriber.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from agentduet import SendWAMessage, SessionManager, new_session_id

from wa_subscriber import load_wa_subscriber, log_discovered

logger = logging.getLogger(__name__)

WA_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v23.0")


def extract_wa_text(payload: dict) -> str:
    """Best-effort text body from a WhatsApp Cloud API webhook payload."""
    try:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for msg in value.get("messages") or []:
                    if msg.get("type") == "text":
                        return (msg.get("text") or {}).get("body") or ""
                    if "text" in msg:
                        return str(msg["text"])
    except Exception:
        logger.debug("WA payload parse failed", exc_info=True)

    text = payload.get("text") or payload.get("body") or ""
    if isinstance(text, dict):
        return text.get("body") or json.dumps(text)
    return str(text) if text else json.dumps(payload)[:500]


class WhatsAppSender:
    def __init__(self, sm: SessionManager, default_subscriber: str | None = None):
        self._sm = sm
        self._wa_subscriber = default_subscriber or load_wa_subscriber()

    @property
    def has_subscriber(self) -> bool:
        return bool(self._wa_subscriber)

    def remember_wa_subscriber(self, subscriber: str) -> None:
        """Call only from @sm.on_incoming_message — never from voice calls."""
        if not subscriber:
            return
        was_new = not bool(self._wa_subscriber)
        self._wa_subscriber = subscriber
        log_discovered(subscriber, was_new=was_new)

    async def send_text(self, to_whatsapp: str, body: str) -> dict[str, Any]:
        subscriber = self._wa_subscriber
        if not subscriber:
            return {
                "success": False,
                "error_code": "NO_WA_SUBSCRIBER",
                "error_message": (
                    "WhatsApp inbox id not set yet. Keep the agent running and send any "
                    "message to your business WhatsApp once — the id is learned "
                    "automatically and saved to .wa_subscriber. Then try again."
                ),
            }

        if not (to_whatsapp or "").strip():
            return {
                "success": False,
                "error_code": "NO_RECIPIENT",
                "error_message": "Driver WhatsApp number is empty",
            }

        # Docs use E.164 with '+'; some providers accept digits only — try both.
        raw = to_whatsapp.strip()
        candidates: list[str] = []
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
                logger.info(
                    "WhatsApp sent to %s via subscriber %s response=%s",
                    to,
                    subscriber,
                    result.response_content,
                )
                return {
                    "success": True,
                    "to": to,
                    "response_content": result.response_content,
                }

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
