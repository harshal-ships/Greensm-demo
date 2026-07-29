"""Inbound call attach + voice / WhatsApp message handlers (AgentDuet 1.0.0b9)."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Deque

from agentduet import (
    Call,
    IncomingCallNotification,
    IncomingMessage,
    SessionManager,
    new_session_id,
)
from agentduet.events import Network
from agentduet.exceptions import CallNotFoundError
from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient

from nova_session import NovaSonicIntegration
from pipedrive import PipedriveClient
from ticket_registry import TicketRegistry
from tools import LostItemTools
from whatsapp import WhatsAppSender, extract_wa_text

logger = logging.getLogger(__name__)

# Docs warn about inbound message redelivery — keep a small recent-id window.
_RECENT_MSG_IDS: Deque[str] = deque(maxlen=256)
_SEEN_MSG_IDS: set[str] = set()


def _remember_msg_id(msg_id: str) -> bool:
    """Return True if this message id was already processed (skip)."""
    if not msg_id:
        return False
    if msg_id in _SEEN_MSG_IDS:
        return True
    if len(_RECENT_MSG_IDS) == _RECENT_MSG_IDS.maxlen:
        old = _RECENT_MSG_IDS[0]
        _SEEN_MSG_IDS.discard(old)
    _RECENT_MSG_IDS.append(msg_id)
    _SEEN_MSG_IDS.add(msg_id)
    return False


async def attach_inbound_call(
    sm: SessionManager, noti: IncomingCallNotification
) -> Call:
    """Open a session on the call's subscriber and claim the inbound call.

    On ``CallNotFoundError`` the call is gone — do not open a new session and
    retry (SDK: the notification is stale).
    """
    session = await sm.open_session(new_session_id(), noti.subscriber)
    try:
        return await session.process_call(noti)
    except CallNotFoundError:
        logger.warning(
            "Call %s not found (stale notification) — skipping",
            noti.call_id,
        )
        raise


async def handle_voice_call(
    sm: SessionManager,
    noti: IncomingCallNotification,
    bedrock_client: BedrockRuntimeClient,
    pipedrive: PipedriveClient,
    registry: TicketRegistry,
    wa_sender: WhatsAppSender,
) -> None:
    channel = "WhatsApp call" if noti.network == Network.WA else "Phone call"
    logger.info("%s %s from %s", channel, noti.call_id, noti.participant.value)

    # Per-call tools so concurrent calls do not share pending / caller state.
    tools = LostItemTools(
        pipedrive=pipedrive,
        registry=registry,
        send_whatsapp=wa_sender.send_text,
        caller_phone=noti.participant.value,
    )
    # Never set WA subscriber from a voice call (TELCO ≠ Meta inbox id).

    try:
        call = await attach_inbound_call(sm, noti)
    except CallNotFoundError:
        return
    except Exception:
        logger.exception("Failed to attach call %s", noti.call_id)
        return

    nova = NovaSonicIntegration(call, bedrock_client, tools)
    loop = asyncio.get_running_loop()

    @call.on_hangup
    def on_hangup(_payload: object = None) -> None:
        # Hangup runs in a worker thread — same pattern as driver-payment.
        asyncio.run_coroutine_threadsafe(nova.shutdown(), loop)

    try:
        answer_task = asyncio.create_task(call.answer())
        prepare_task = asyncio.create_task(nova.prepare())

        answered = False
        try:
            answered = bool(await answer_task)
        except Exception:
            logger.exception("call.answer() raised for %s", noti.call_id)
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
        # Safety net if hangup never fired; shutdown() is idempotent.
        try:
            await nova.shutdown()
        except Exception:
            pass


async def handle_incoming_message(
    msg: IncomingMessage,
    pipedrive: PipedriveClient,
    registry: TicketRegistry,
    wa_sender: WhatsAppSender | None = None,
) -> None:
    """Learn WA subscriber; log driver replies onto the matching Pipedrive deal."""
    msg_id = getattr(msg, "id", None) or ""
    if _remember_msg_id(str(msg_id)):
        logger.debug("Skipping redelivered WhatsApp message id=%s", msg_id)
        return

    # Docs: msg.subscriber is the WhatsApp business identity for this channel.
    if wa_sender is not None:
        wa_sender.remember_wa_subscriber(msg.subscriber)

    phone = msg.participant.value
    meta = registry.find(phone)
    payload = msg.payload if isinstance(msg.payload, dict) else {}
    text = extract_wa_text(payload)
    logger.info("Incoming WhatsApp from %s: %s", phone, text[:200])

    if not meta:
        logger.info("No open lost-item ticket mapped to %s — ignoring", phone)
        return

    safe_text = (text or "").replace("<", "&lt;").replace(">", "&gt;")
    note = (
        f"<p><b>Driver reply</b> from {phone} (ride {meta['ride_id']}): {safe_text}</p>"
    )
    try:
        pipedrive.add_note_to_deal(meta["deal_id"], note, html=True)
        logger.info("Logged driver reply onto deal %s", meta["deal_id"])
    except Exception:
        logger.exception("Failed to log driver reply to Pipedrive")
