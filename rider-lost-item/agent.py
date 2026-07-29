"""
GreenSM rider lost-item voice agent (AgentDuet 1.0.0b9).

Voice → notify driver on WhatsApp during the call → Pipedrive ticket on hangup.
AI (Nova) handles dialogue + tool calls; code owns rides, WA, and CRM.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from agentduet import (
    CallAudioConfig,
    InboundCallMode,
    IncomingCallNotification,
    IncomingMessage,
    SessionManager,
    SessionManagerConfig,
    TriggerConditionsBuilder,
)
from dotenv import load_dotenv

from audio import AGENTDUET_SAMPLE_RATE
from handlers import handle_incoming_message, handle_voice_call
from nova_session import MODEL_ID, create_bedrock_client
from pipedrive import PipedriveClient
from ticket_registry import TicketRegistry
from wa_subscriber import log_startup_status
from whatsapp import WhatsAppSender

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# Quiet AgentDuet internals; keep [USER] / [ASSISTANT] from this app.
logging.getLogger("agentduet").setLevel(logging.WARNING)


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
            log_startup_status(wa_sender.has_subscriber)

            logger.info(
                "GreenSM lost-item agent connected (model=%s pipedrive_mock=%s)",
                MODEL_ID,
                pipedrive.mock_mode,
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
                        sm,
                        noti,
                        bedrock_client,
                        pipedrive,
                        registry,
                        wa_sender,
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
