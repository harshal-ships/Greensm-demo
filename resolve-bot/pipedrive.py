"""Pipedrive CRM client for GreenSM Resolve support cases."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)

COMPLAINT_LABELS: dict[str, str] = {
    "rude_driver": "Rude / unprofessional driver",
    "unsafe_driving": "Unsafe driving",
    "dirty_vehicle": "Dirty / smelly vehicle",
    "bad_route": "Bad or long route",
    "refused_stop": "Refused reasonable stop",
    "abusive_rider": "Abusive / threatening rider",
    "vehicle_damage": "Vehicle damage by rider",
}


def _esc(value: Any) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class PipedriveClient:
    """Creates lost-item or complaint deals, or mocks when unconfigured."""

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

    def _stage_id_for(self, case_type: str) -> int | None:
        if case_type == "lost":
            raw = os.getenv("PIPEDRIVE_LOST_STAGE_ID", "").strip()
        else:
            raw = os.getenv("PIPEDRIVE_COMPLAINT_STAGE_ID", "").strip()
        if not raw:
            raw = os.getenv("PIPEDRIVE_STAGE_ID", "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            logger.warning("Ignoring invalid stage id for case_type=%s: %r", case_type, raw)
            return None

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

    def create_case(
        self,
        *,
        case_type: str,
        caller_role: str,
        ride: dict[str, Any],
        caller_phone: str,
        caller_name: str | None = None,
        item_description: str | None = None,
        complaint_category: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        ride_id = ride.get("ride_id") or "unknown"
        role_label = "Rider" if caller_role == "rider" else "Driver"

        if case_type == "lost":
            item = (item_description or "item").strip()[:50]
            title = f"Lost item: {item} ({ride_id})"
            case_line = f"<p><b>Case</b><br/>Lost item · {role_label}</p>"
            detail_line = f"<p><b>Item</b><br/>{_esc(item_description)}</p>"
        else:
            label = COMPLAINT_LABELS.get(
                complaint_category or "", complaint_category or "Complaint"
            )
            title = f"Complaint: {label} ({ride_id})"
            case_line = f"<p><b>Case</b><br/>Complaint · {role_label}</p>"
            detail_line = (
                f"<p><b>Category</b><br/>{_esc(label)}</p>"
                f"<p><b>Summary</b><br/>{_esc(description)}</p>"
            )

        note_body = (
            "<p><b>GreenSM Resolve report</b></p>"
            f"{case_line}"
            f"{detail_line}"
            f"<p><b>Ride</b><br/>"
            f"{_esc(ride_id)} · {_esc(ride.get('completed_at'))}<br/>"
            f"{_esc(ride.get('pickup'))} → {_esc(ride.get('dropoff'))}<br/>"
            f"{_esc(ride.get('vehicle'))}</p>"
            f"<p><b>Caller</b><br/>"
            f"{_esc(caller_name or ride.get('rider_name'))} · {_esc(caller_phone)} "
            f"({role_label})</p>"
            f"<p><b>Driver on ride</b><br/>{_esc(ride.get('driver_name'))}</p>"
            "<p><i>Created automatically by the GreenSM Resolve voice agent.</i></p>"
        )

        if self.mock_mode:
            deal_id = f"mock-{uuid.uuid4().hex[:8]}"
            logger.info("[MOCK Pipedrive] deal_id=%s title=%s", deal_id, title)
            return {"deal_id": deal_id, "title": title, "mock": True, "url": None}

        person_label = caller_name or (role_label)
        person = self._request(
            "POST",
            "/persons",
            {
                "name": person_label,
                "phone": [{"value": caller_phone or "", "primary": True}],
            },
        )
        person_id = person.get("id")

        deal_payload: dict[str, Any] = {"title": title, "person_id": person_id}
        stage_id = self._stage_id_for(case_type)
        if stage_id is not None:
            deal_payload["stage_id"] = stage_id

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

        logger.info(
            "Created Pipedrive deal id=%s title=%s case_type=%s role=%s",
            deal_id,
            title,
            case_type,
            caller_role,
        )
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
