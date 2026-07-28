"""Pipedrive CRM client for GreenSM lost-item tickets."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)


class PipedriveClient:
    """Creates lost-item deals (tickets) in Pipedrive, or mocks when unconfigured."""

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

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        params = {"api_token": self.api_token}
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
        callback_number: str,
        rider_name: str | None = None,
    ) -> dict[str, Any]:
        safe_item = (item_description or "item")[:60]
        title = f"Lost Item — {ride.get('ride_id')}: {safe_item}"
        note_body = (
            f"<p><b>GreenSM Lost Item Report</b></p>"
            f"<p><b>Ride:</b> {ride.get('ride_id')}<br/>"
            f"<b>When:</b> {ride.get('completed_at')}<br/>"
            f"<b>Route:</b> {ride.get('pickup')} → {ride.get('dropoff')}<br/>"
            f"<b>Driver:</b> {ride.get('driver_name')} ({ride.get('vehicle')})<br/>"
            f"<b>Driver WhatsApp:</b> {ride.get('driver_whatsapp')}<br/>"
            f"<b>Item:</b> {item_description}<br/>"
            f"<b>Rider contact on file:</b> {callback_number}<br/>"
            f"<b>Rider phone on ride:</b> {ride.get('rider_phone')}</p>"
        )

        if self.mock_mode:
            deal_id = f"mock-{uuid.uuid4().hex[:8]}"
            logger.info("[MOCK Pipedrive] deal_id=%s title=%s", deal_id, title)
            return {
                "deal_id": deal_id,
                "title": title,
                "mock": True,
                "url": None,
            }

        person_name = rider_name or ride.get("rider_name") or "GreenSM Rider"
        person = self._request(
            "POST",
            "/persons",
            {
                "name": person_name,
                "phone": [{"value": callback_number, "primary": True}],
            },
        )
        person_id = person.get("id")

        deal_payload: dict[str, Any] = {
            "title": title,
            "person_id": person_id,
        }
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
                {"deal_id": deal_id, "content": note_body},
            )
        except Exception:
            # Deal exists — do not fail the whole ticket if the note fails.
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

    def add_note_to_deal(self, deal_id: str | int, content: str) -> dict[str, Any]:
        if self.mock_mode or str(deal_id).startswith("mock-"):
            logger.info("[MOCK Pipedrive] note on %s: %s", deal_id, content[:200])
            return {"mock": True, "deal_id": deal_id}

        try:
            numeric_id = int(deal_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid Pipedrive deal_id: {deal_id!r}") from exc

        safe = (content or "").replace("<", "&lt;").replace(">", "&gt;")
        note = self._request(
            "POST",
            "/notes",
            {"deal_id": numeric_id, "content": f"<p>{safe}</p>"},
        )
        return {"mock": False, "note_id": note.get("id"), "deal_id": deal_id}
