"""Resolve and cache the AgentDuet WhatsApp business subscriber id.

New users do NOT need to know this value up front.
AgentDuet only exposes it on an inbound WhatsApp message (msg.subscriber).
We save it to .wa_subscriber so the next run works with zero config.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).resolve().parent / ".wa_subscriber"


def _looks_like_phone_not_inbox_id(value: str) -> bool:
    """Reject display/TELCO numbers people often paste by mistake."""
    v = value.strip()
    if v.startswith("+"):
        return True
    # Short digit strings that look like country+national numbers, not Meta ids
    if v.isdigit() and len(v) <= 12:
        return True
    return False


def load_wa_subscriber() -> str:
    """Env wins; otherwise reuse id learned from the first inbound WhatsApp."""
    env = os.getenv("AGENTDUET_WA_SUBSCRIBER", "").strip().strip('"').strip("'")
    if env:
        if _looks_like_phone_not_inbox_id(env):
            logger.warning(
                "AGENTDUET_WA_SUBSCRIBER=%r looks like a phone number, not the "
                "WhatsApp inbox id. Ignoring — use auto-discover instead "
                "(text your business WhatsApp once while the agent is running).",
                env,
            )
        else:
            return env
    try:
        if CACHE_PATH.is_file():
            cached = CACHE_PATH.read_text(encoding="utf-8").strip()
            if cached and not _looks_like_phone_not_inbox_id(cached):
                logger.info("Loaded WhatsApp subscriber from %s", CACHE_PATH.name)
                return cached
    except OSError:
        logger.exception("Could not read %s", CACHE_PATH)
    return ""


def save_wa_subscriber(subscriber: str) -> None:
    sub = (subscriber or "").strip()
    if not sub:
        return
    try:
        CACHE_PATH.write_text(sub + "\n", encoding="utf-8")
    except OSError:
        logger.exception("Could not write %s", CACHE_PATH)


def log_startup_status(has_subscriber: bool) -> None:
    if has_subscriber:
        logger.info("WhatsApp subscriber ready (outbound driver alerts enabled)")
        return

    logger.warning(
        "FIRST-RUN WhatsApp setup (no AGENTDUET_WA_SUBSCRIBER needed):\n"
        "  1. Keep this agent running.\n"
        "  2. From any phone, send 'hi' to your AgentDuet *business* WhatsApp number.\n"
        "  3. This process will print READY and save the id to %s.\n"
        "  4. Then place the demo voice call — driver WhatsApp will work.\n"
        "  (Optional later: copy that id into .env as AGENTDUET_WA_SUBSCRIBER=...)",
        CACHE_PATH.name,
    )


def log_discovered(subscriber: str, *, was_new: bool) -> None:
    save_wa_subscriber(subscriber)
    if was_new:
        logger.info(
            "READY — WhatsApp inbox id learned: %s\n"
            "  Saved to %s for next runs.\n"
            "  Optional: AGENTDUET_WA_SUBSCRIBER=%s in .env",
            subscriber,
            CACHE_PATH.name,
            subscriber,
        )
    else:
        logger.info("WhatsApp business subscriber set to %s", subscriber)
