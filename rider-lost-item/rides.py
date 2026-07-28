"""Mock recent rides lookup for the GreenSM lost-item demo."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RIDES_PATH = Path(__file__).resolve().parent / "data" / "recent_rides.json"

# Common speech-to-text mishears for "GRN"
_RIDE_PREFIX_FIXES = (
    ("GIN", "GRN"),
    ("GREEN", "GRN"),
    ("GREN", "GRN"),
    ("JRN", "GRN"),
    ("JIN", "GRN"),
    ("CRM", "GRN"),
)


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def normalize_ride_id(raw: str | None) -> str | None:
    """Normalize spoken/typed ride ids toward GRN-##### form."""
    if not raw:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if not s:
        return None

    for wrong, right in _RIDE_PREFIX_FIXES:
        if s.startswith(wrong):
            s = right + s[len(wrong) :]
            break

    # GRN88421 → GRN-88421
    m = re.match(r"^(GRN)(\d{4,6})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # Already GRN-88421 after stripping then reinsert
    m = re.match(r"^(GRN)[-]?(\d{4,6})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    return s


def load_rides() -> list[dict[str, Any]]:
    try:
        data = json.loads(RIDES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("Rides file missing: %s", RIDES_PATH)
        return []
    except json.JSONDecodeError:
        logger.exception("Invalid rides JSON: %s", RIDES_PATH)
        return []
    if not isinstance(data, list):
        logger.error("Rides JSON must be a list")
        return []
    return data


def lookup_rides(
    *,
    rider_phone: str | None = None,
    ride_id: str | None = None,
    pickup_hint: str | None = None,
    dropoff_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Return matching rides (most specific filters first)."""
    rides = load_rides()
    matches = rides

    if ride_id:
        rid = normalize_ride_id(ride_id) or ride_id.strip().upper()
        exact = [r for r in matches if r.get("ride_id", "").upper() == rid]
        if exact:
            return exact

        # Fuzzy: compare digits-only suffix (88421) against known ids
        digits = re.sub(r"\D", "", rid)
        if len(digits) >= 4:
            fuzzy = [
                r
                for r in matches
                if re.sub(r"\D", "", r.get("ride_id", "")).endswith(digits[-5:])
            ]
            if fuzzy:
                logger.info(
                    "Fuzzy ride match input=%s normalized=%s → %s",
                    ride_id,
                    rid,
                    [r["ride_id"] for r in fuzzy],
                )
                return fuzzy
        return []

    if rider_phone:
        phone = _normalize_phone(rider_phone)
        if len(phone) >= 8:
            matches = [
                r
                for r in matches
                if _normalize_phone(r.get("rider_phone", "")).endswith(phone[-8:])
            ]
        elif phone:
            matches = [
                r
                for r in matches
                if phone in _normalize_phone(r.get("rider_phone", ""))
            ]

    if pickup_hint:
        hint = pickup_hint.lower().strip()
        if hint:
            matches = [r for r in matches if hint in (r.get("pickup") or "").lower()]

    if dropoff_hint:
        hint = dropoff_hint.lower().strip()
        if hint:
            matches = [r for r in matches if hint in (r.get("dropoff") or "").lower()]

    return matches


def ride_summary(ride: dict[str, Any]) -> str:
    return (
        f"Ride {ride.get('ride_id')} on {ride.get('completed_at')}: "
        f"{ride.get('pickup')} → {ride.get('dropoff')}, "
        f"driver {ride.get('driver_name')} ({ride.get('vehicle')})."
    )
