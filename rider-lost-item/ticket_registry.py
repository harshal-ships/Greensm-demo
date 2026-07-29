"""In-memory map: driver WhatsApp → open Pipedrive deal (demo process lifetime)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from phone import normalize_phone


@dataclass
class TicketRegistry:
    """Correlate driver WhatsApp replies to the deal created after hangup."""

    by_driver_whatsapp: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register(self, driver_whatsapp: str, deal_id: Any, ride_id: str) -> None:
        key = normalize_phone(driver_whatsapp)
        if not key:
            return
        meta = {
            "deal_id": deal_id,
            "ride_id": ride_id,
            "driver_whatsapp": driver_whatsapp,
        }
        self.by_driver_whatsapp[key] = meta
        if len(key) >= 8:
            self.by_driver_whatsapp[key[-8:]] = meta

    def find(self, participant_phone: str) -> dict[str, Any] | None:
        key = normalize_phone(participant_phone)
        if not key:
            return None
        if key in self.by_driver_whatsapp:
            return self.by_driver_whatsapp[key]
        for stored, meta in self.by_driver_whatsapp.items():
            if len(key) >= 8 and len(stored) >= 8:
                if stored.endswith(key[-8:]) or key.endswith(stored[-8:]):
                    return meta
        return None
