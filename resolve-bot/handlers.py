"""Inbound call attach + voice call handler (AgentDuet 1.0.0b9)."""

from __future__ import annotations

import asyncio
import logging

from agentduet import (
    Call,
    IncomingCallNotification,
    SessionManager,
    new_session_id,
)
from agentduet.events import Network
from agentduet.exceptions import CallNotFoundError
from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient

from nova_session import NovaSonicIntegration
from pipedrive import PipedriveClient
from tools import SupportTools

logger = logging.getLogger(__name__)


async def attach_inbound_call(
    sm: SessionManager, noti: IncomingCallNotification
) -> Call:
    """Open a session on the call's subscriber and claim the inbound call."""
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
) -> None:
    channel = "WhatsApp call" if noti.network == Network.WA else "Phone call"
    logger.info("%s %s from %s", channel, noti.call_id, noti.participant.value)

    tools = SupportTools(
        pipedrive=pipedrive,
        caller_phone=noti.participant.value,
    )

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
        logger.exception("Error in Resolve voice integration")
        try:
            await nova.cancel()
        except Exception:
            pass
        try:
            await call.close()
        except Exception:
            pass
    finally:
        try:
            await nova.shutdown()
        except Exception:
            pass
