"""Pipedrive CRM client for GreenSM lost-item tickets."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)


def _esc(value: Any) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _human_wa_error(error: str | None) -> str:
    """Keep Pipedrive notes readable — never dump SDK / env setup text."""
    raw = (error or "").strip()
    low = raw.lower()
    if not raw:
        return "Could not send WhatsApp"
    if "no_wa_subscriber" in low or "agentduet_wa_subscriber" in low or "subscriber" in low:
        return "WhatsApp not set up yet (business inbox)"
    if "inbox.notfound" in low or "whatsapp number not found" in low:
        return "WhatsApp inbox not found"
    if "timeout" in low:
        return "WhatsApp timed out"
    # One short line max
    return raw.split("\n")[0][:120]


class PipedriveClient:
    """Creates simple lost-item deals, or mocks when unconfigured."""

    def __init__(
        self,
        api_token: str | None = None,
        company_domain: str | None = None,
    ):
        self.api_token = api_token or os.getenv("PIPEDRIVE_API_TOKEN", "").strip()
        self.company_domain = (
            company_domain or os.getenv("PIPEDRIVE_COMPANY_DOMAIN", "").strip()
        )
        self.mock_mode = not bool(self.api_token)
        self.timeout = float(os.getenv("PIPEDRIVE_TIMEOUT_SECONDS", "30"))

        if self.company_domain:
            self.base_url = f"https://{self.company_domain}.pipedrive.com/api/v1"
        else:
            self.base_url = "https://api.pipedrive.com/v1"

        if self.mock_mode:
            logger.warning(
                "PIPEDRIVE_API_TOKEN not set — Pipedrive writes will be mocked"
            )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        query: dict | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        params: dict[str, Any] = {"api_token": self.api_token}
        if query:
            params.update(query)
        try:
            resp = requests.request(
                method,
                url,
                params=params,
                json=payload,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise RuntimeError(f"Pipedrive timeout on {path}") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Pipedrive network error on {path}: {exc}") from exc

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Pipedrive HTTP {resp.status_code} on {path}: {resp.text[:300]}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Pipedrive returned non-JSON on {path}") from exc

        if not data.get("success", True):
            raise RuntimeError(f"Pipedrive error on {path}: {data}")
        return data.get("data") or {}

    def create_lost_item_ticket(
        self,
        *,
        ride: dict[str, Any],
        item_description: str,
        rider_phone: str,
        rider_name: str | None = None,
    ) -> dict[str, Any]:
        item = (item_description or "item").strip()[:50]
        ride_id = ride.get("ride_id") or "unknown"
        title = f"Lost item: {item} ({ride_id})"

        note_body = (
            "<p><b>GreenSM lost-item report</b></p>"
            f"<p><b>Item</b><br/>{_esc(item_description)}</p>"
            f"<p><b>Ride</b><br/>"
            f"{_esc(ride_id)} · {_esc(ride.get('completed_at'))}<br/>"
            f"{_esc(ride.get('pickup'))} → {_esc(ride.get('dropoff'))}<br/>"
            f"{_esc(ride.get('vehicle'))}</p>"
            f"<p><b>Rider</b><br/>"
            f"{_esc(rider_name or ride.get('rider_name'))} · {_esc(rider_phone)}</p>"
            f"<p><b>Driver</b><br/>"
            f"{_esc(ride.get('driver_name'))} · WhatsApp {_esc(ride.get('driver_whatsapp'))}</p>"
            "<p><i>Created automatically by the GreenSM voice agent.</i></p>"
        )

        if self.mock_mode:
            deal_id = f"mock-{uuid.uuid4().hex[:8]}"
            logger.info("[MOCK Pipedrive] deal_id=%s title=%s", deal_id, title)
            return {"deal_id": deal_id, "title": title, "mock": True, "url": None}

        person = self._request(
            "POST",
            "/persons",
            {
                "name": rider_name or ride.get("rider_name") or "Rider",
                "phone": [{"value": rider_phone or "", "primary": True}],
            },
        )
        person_id = person.get("id")

        deal_payload: dict[str, Any] = {"title": title, "person_id": person_id}
        stage_id = os.getenv("PIPEDRIVE_STAGE_ID", "").strip()
        if stage_id:
            try:
                deal_payload["stage_id"] = int(stage_id)
            except ValueError:
                logger.warning("Ignoring invalid PIPEDRIVE_STAGE_ID=%r", stage_id)

        deal = self._request("POST", "/deals", deal_payload)
        deal_id = deal.get("id")
        if not deal_id:
            raise RuntimeError(f"Pipedrive deal create returned no id: {deal}")

        try:
            self._request(
                "POST",
                "/notes",
                {"deal_id": deal_id, "content": note_body, "pinned_to_deal_flag": 1},
            )
        except Exception:
            logger.exception("Pipedrive note failed for deal %s (deal kept)", deal_id)

        url = None
        if self.company_domain and deal_id:
            url = f"https://{self.company_domain}.pipedrive.com/deal/{deal_id}"

        logger.info("Created Pipedrive deal id=%s title=%s", deal_id, title)
        return {
            "deal_id": deal_id,
            "title": title,
            "mock": False,
            "url": url,
            "person_id": person_id,
        }

    def add_note_to_deal(
        self,
        deal_id: str | int,
        content: str,
        *,
        html: bool = False,
    ) -> dict[str, Any]:
        if self.mock_mode or str(deal_id).startswith("mock-"):
            logger.info("[MOCK Pipedrive] note on %s: %s", deal_id, content[:200])
            return {"mock": True, "deal_id": deal_id}

        try:
            numeric_id = int(deal_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid Pipedrive deal_id: {deal_id!r}") from exc

        body = content if html else f"<p>{_esc(content or '')}</p>"
        note = self._request(
            "POST", "/notes", {"deal_id": numeric_id, "content": body}
        )
        return {"mock": False, "note_id": note.get("id"), "deal_id": deal_id}

    def mark_whatsapp_status(
        self,
        deal_id: str | int,
        *,
        ok: bool,
        driver_name: str,
        driver_whatsapp: str,
        item: str,
        error: str | None = None,
    ) -> None:
        """Human-readable timeline note — no Meta message ids or SDK dumps."""
        if ok:
            content = (
                f"<p><b>WhatsApp sent</b> to {_esc(driver_name)} "
                f"({_esc(driver_whatsapp)}) about {_esc(item)}.</p>"
            )
        else:
            content = (
                f"<p><b>WhatsApp not sent</b> to {_esc(driver_name)} "
                f"({_esc(driver_whatsapp)}).<br/>"
                f"{_esc(_human_wa_error(error))}</p>"
            )
        try:
            self.add_note_to_deal(deal_id, content, html=True)
        except Exception:
            logger.exception("Failed to log WhatsApp status on deal %s", deal_id)
