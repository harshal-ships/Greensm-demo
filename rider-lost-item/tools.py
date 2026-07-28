"""Nova Sonic tool schemas and handlers for lost-item filing."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pipedrive_client import PipedriveClient
from rides import lookup_rides, normalize_ride_id, ride_summary

logger = logging.getLogger(__name__)


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def _parse_tool_args(tool_content: dict) -> dict[str, Any]:
    raw_input = tool_content.get("content") or tool_content.get("input") or "{}"
    if isinstance(raw_input, dict):
        return raw_input
    if not isinstance(raw_input, str):
        return {}
    try:
        parsed = json.loads(raw_input) if raw_input.strip() else {}
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        logger.warning("Tool args were not valid JSON: %r", raw_input[:200])
        return {}


@dataclass
class TicketRegistry:
    """Maps driver WhatsApp numbers → open Pipedrive deal ids (demo in-memory)."""

    by_driver_whatsapp: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register(self, driver_whatsapp: str, deal_id: Any, ride_id: str) -> None:
        key = _normalize_phone(driver_whatsapp)
        if not key:
            return
        meta = {
            "deal_id": deal_id,
            "ride_id": ride_id,
            "driver_whatsapp": driver_whatsapp,
        }
        self.by_driver_whatsapp[key] = meta
        # Also index last 8 digits for local-format replies
        if len(key) >= 8:
            self.by_driver_whatsapp[key[-8:]] = meta

    def find(self, participant_phone: str) -> dict[str, Any] | None:
        key = _normalize_phone(participant_phone)
        if not key:
            return None
        if key in self.by_driver_whatsapp:
            return self.by_driver_whatsapp[key]
        for stored, meta in self.by_driver_whatsapp.items():
            if len(key) >= 8 and len(stored) >= 8:
                if stored.endswith(key[-8:]) or key.endswith(stored[-8:]):
                    return meta
        return None


TOOL_SCHEMAS = [
    {
        "toolSpec": {
            "name": "lookupRecentRide",
            "description": (
                "Look up a recent GreenSM ride to identify the driver. "
                "Use after the rider describes pickup, dropoff, ride id, or phone. "
                "Ride ids look like GRN-88421 (spoken as G-R-N eight eight four two one)."
            ),
            "inputSchema": {
                "json": json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "rider_phone": {
                                "type": "string",
                                "description": "Rider phone number if known",
                            },
                            "ride_id": {
                                "type": "string",
                                "description": "GreenSM ride id like GRN-88421",
                            },
                            "pickup_hint": {
                                "type": "string",
                                "description": "Partial pickup place name",
                            },
                            "dropoff_hint": {
                                "type": "string",
                                "description": "Partial dropoff place name",
                            },
                        },
                        "required": [],
                    }
                )
            },
        }
    },
    {
        "toolSpec": {
            "name": "fileLostItemReport",
            "description": (
                "After the rider confirms the ride and item details, create a Pipedrive "
                "ticket and WhatsApp the driver. Do not ask for a callback number. "
                "Always tell the rider whether the ticket and WhatsApp succeeded."
            ),
            "inputSchema": {
                "json": json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "ride_id": {
                                "type": "string",
                                "description": "Confirmed GreenSM ride id",
                            },
                            "item_description": {
                                "type": "string",
                                "description": "What the rider lost",
                            },
                            "rider_name": {
                                "type": "string",
                                "description": "Rider name if known",
                            },
                        },
                        "required": [
                            "ride_id",
                            "item_description",
                        ],
                    }
                )
            },
        }
    },
]


class LostItemTools:
    def __init__(
        self,
        pipedrive: PipedriveClient,
        registry: TicketRegistry,
        send_whatsapp: Callable[[str, str], Awaitable[dict[str, Any]]],
    ):
        self.pipedrive = pipedrive
        self.registry = registry
        self.send_whatsapp = send_whatsapp

    async def execute(self, tool_name: str, tool_content: dict) -> dict[str, Any]:
        try:
            args = _parse_tool_args(tool_content)
            logger.info("Tool %s args=%s", tool_name, args)

            if tool_name == "lookupRecentRide":
                return self._lookup(args)
            if tool_name == "fileLostItemReport":
                return await self._file_report(args)
            return {
                "ok": False,
                "error": f"Unknown tool: {tool_name}",
                "agent_speak_summary": (
                    "Something went wrong looking that up. Please say the ride details again."
                ),
            }
        except Exception as exc:
            logger.exception("Unhandled tool error in %s", tool_name)
            return {
                "ok": False,
                "error": str(exc),
                "agent_speak_summary": (
                    "I hit a technical issue. Please repeat your ride details and I will try again."
                ),
            }

    def _lookup(self, args: dict) -> dict[str, Any]:
        raw_ride_id = args.get("ride_id")
        normalized = normalize_ride_id(raw_ride_id) if raw_ride_id else None

        matches = lookup_rides(
            rider_phone=args.get("rider_phone"),
            ride_id=raw_ride_id,
            pickup_hint=args.get("pickup_hint"),
            dropoff_hint=args.get("dropoff_hint"),
        )

        if not matches:
            hint = ""
            if raw_ride_id:
                hint = (
                    f" I heard ride id {raw_ride_id}"
                    + (f" (normalized {normalized})" if normalized else "")
                    + "."
                )
            return {
                "ok": True,
                "count": 0,
                "rides": [],
                "message": (
                    "No matching recent rides found."
                    + hint
                    + " Ask for pickup, dropoff, or the ride id again as G-R-N plus digits."
                ),
                "agent_speak_summary": (
                    "I could not find that ride. Please tell me the pickup and dropoff, "
                    "or say the ride id slowly as G R N, then the numbers."
                ),
            }

        rides_out = []
        for r in matches[:5]:
            rides_out.append(
                {
                    "ride_id": r["ride_id"],
                    "completed_at": r["completed_at"],
                    "pickup": r["pickup"],
                    "dropoff": r["dropoff"],
                    "driver_name": r["driver_name"],
                    "vehicle": r["vehicle"],
                    "summary": ride_summary(r),
                }
            )

        if len(rides_out) == 1:
            speak = (
                f"I found ride {rides_out[0]['ride_id']}: "
                f"{rides_out[0]['pickup']} to {rides_out[0]['dropoff']}, "
                f"driver {rides_out[0]['driver_name']}. Please confirm this is correct."
            )
        else:
            speak = (
                f"I found {len(rides_out)} matching rides. "
                "Please confirm which ride id is yours."
            )

        return {
            "ok": True,
            "count": len(rides_out),
            "rides": rides_out,
            "message": "Confirm the correct ride with the rider before filing.",
            "agent_speak_summary": speak,
        }

    async def _file_report(self, args: dict) -> dict[str, Any]:
        ride_id = normalize_ride_id(args.get("ride_id")) or (
            (args.get("ride_id") or "").strip().upper()
        )
        item = (args.get("item_description") or "").strip()
        rider_name = (args.get("rider_name") or "").strip() or None

        if not ride_id or not item:
            return {
                "ok": False,
                "error": "ride_id and item_description are required",
                "agent_speak_summary": (
                    "I still need the ride id and what you lost before I can file the report."
                ),
            }

        matches = lookup_rides(ride_id=ride_id)
        if not matches:
            return {
                "ok": False,
                "error": f"Ride {ride_id} not found",
                "agent_speak_summary": (
                    f"I could not find ride {ride_id}. "
                    "Please confirm the ride id or pickup and dropoff."
                ),
            }

        ride = matches[0]
        # Demo: use the number on the ride record — do not ask the caller for a callback.
        contact_phone = (ride.get("rider_phone") or "").strip() or "n/a"

        ticket = None
        pipedrive_ok = False
        pipedrive_error = None
        try:
            ticket = self.pipedrive.create_lost_item_ticket(
                ride=ride,
                item_description=item,
                callback_number=contact_phone,
                rider_name=rider_name or ride.get("rider_name"),
            )
            pipedrive_ok = True
        except Exception as exc:
            logger.exception("Pipedrive ticket failed")
            pipedrive_error = str(exc)

        wa_body = (
            f"GreenSM Lost Item Alert\n"
            f"Ride: {ride['ride_id']}\n"
            f"Route: {ride['pickup']} → {ride['dropoff']}\n"
            f"Time: {ride['completed_at']}\n"
            f"Item: {item}\n"
            f"Rider: {ride.get('rider_name', 'n/a')}\n"
            f"Please check the vehicle and reply to this chat if you find it. Thank you!"
        )

        whatsapp_ok = False
        whatsapp_error = None
        try:
            wa_result = await self.send_whatsapp(ride["driver_whatsapp"], wa_body)
            whatsapp_ok = bool(wa_result.get("success"))
            if not whatsapp_ok:
                raw_err = (
                    wa_result.get("error_message")
                    or wa_result.get("error_code")
                    or "WhatsApp send failed"
                )
                whatsapp_error = (
                    raw_err
                    if isinstance(raw_err, str)
                    else json.dumps(raw_err, default=str)
                )
        except Exception as exc:
            logger.exception("WhatsApp to driver failed")
            whatsapp_error = str(exc)

        if pipedrive_ok and ticket:
            try:
                self.registry.register(
                    ride["driver_whatsapp"], ticket["deal_id"], ride["ride_id"]
                )
            except Exception:
                logger.exception("Failed to register ticket for WA reply tracking")

        speak_parts: list[str] = []
        if pipedrive_ok and ticket:
            speak_parts.append(
                f"Your ticket has been created — ticket number {ticket['deal_id']}."
            )
        else:
            speak_parts.append(
                "I could not create the ticket just now, but I will still try to reach the driver."
            )

        if whatsapp_ok:
            speak_parts.append(
                f"I have messaged the driver, {ride['driver_name']}, on WhatsApp "
                f"about your {item}. Thank you for calling GreenSM."
            )
        else:
            speak_parts.append(
                "I could not reach the driver on WhatsApp just now. "
                "Our team will follow up from the ticket. Thank you for calling."
            )

        return {
            "ok": pipedrive_ok or whatsapp_ok,
            "pipedrive_ok": pipedrive_ok,
            "pipedrive_error": pipedrive_error,
            "ticket_id": (ticket or {}).get("deal_id"),
            "whatsapp_ok": whatsapp_ok,
            "whatsapp_error": whatsapp_error,
            "driver_name": ride["driver_name"],
            "ride_id": ride["ride_id"],
            "agent_speak_summary": " ".join(speak_parts),
        }
