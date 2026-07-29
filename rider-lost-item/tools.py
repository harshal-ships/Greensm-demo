"""Nova tool schemas and handlers — deterministic side effects (not the model)."""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from pipedrive import PipedriveClient
from rides import lookup_rides, normalize_ride_id, ride_summary
from ticket_registry import TicketRegistry

logger = logging.getLogger(__name__)

SendWhatsApp = Callable[[str, str], Awaitable[dict[str, Any]]]


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


TOOL_SCHEMAS = [
    {
        "toolSpec": {
            "name": "lookupRecentRide",
            "description": (
                "Look up a recent GreenSM ride to identify the driver. "
                "Use after the rider describes pickup, dropoff, or ride id. "
                "Ride ids look like GRN-88421 (spoken as G-R-N eight eight four two one)."
            ),
            "inputSchema": {
                "json": json.dumps(
                    {
                        "type": "object",
                        "properties": {
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
            "name": "notifyDriverLostItem",
            "description": (
                "After the rider confirms the ride and item, WhatsApp the driver now. "
                "Pipedrive ticket is created automatically when the call ends — "
                "do not invent a ticket number. Do not ask for a callback number. "
                "Always tell the rider whether WhatsApp succeeded."
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
                        "required": ["ride_id", "item_description"],
                    }
                )
            },
        }
    },
]


class LostItemTools:
    """Per-call tool surface: lookup rides, WhatsApp driver, queue Pipedrive for hangup."""

    def __init__(
        self,
        pipedrive: PipedriveClient,
        registry: TicketRegistry,
        send_whatsapp: SendWhatsApp,
        *,
        caller_phone: str = "",
    ):
        self.pipedrive = pipedrive
        self.registry = registry
        self.send_whatsapp = send_whatsapp
        self.caller_phone = (caller_phone or "").strip()
        self._pending_after_call: dict[str, Any] | None = None

    async def execute(self, tool_name: str, tool_content: dict) -> dict[str, Any]:
        try:
            args = _parse_tool_args(tool_content)
            logger.info("Tool %s args=%s", tool_name, args)

            if tool_name == "lookupRecentRide":
                return self._lookup(args)
            if tool_name == "notifyDriverLostItem":
                return await self._notify_driver(args)
            # Backward-compatible alias if an old stream still emits the old name
            if tool_name == "fileLostItemReport":
                return await self._notify_driver(args)
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
            "message": "Confirm the correct ride with the rider before notifying the driver.",
            "agent_speak_summary": speak,
        }

    async def _notify_driver(self, args: dict) -> dict[str, Any]:
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
                    "I still need the ride id and what you lost before I can notify the driver."
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
        contact_phone = self.caller_phone or "unknown"
        display_rider = rider_name or ride.get("rider_name") or "n/a"

        wa_body = (
            f"GreenSM Lost Item Alert\n"
            f"Ride: {ride['ride_id']}\n"
            f"Route: {ride['pickup']} → {ride['dropoff']}\n"
            f"Time: {ride['completed_at']}\n"
            f"Item: {item}\n"
            f"Rider: {display_rider}\n"
            f"Please check the vehicle and reply to this chat if you find it. Thank you!"
        )

        whatsapp_ok = False
        whatsapp_error = None
        wa_result: dict[str, Any] = {}
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

        resp = wa_result.get("response_content")
        if isinstance(resp, dict) and resp.get("messageId"):
            logger.info(
                "WhatsApp messageId=%s (not written to Pipedrive)",
                resp["messageId"],
            )

        self._pending_after_call = {
            "ride": ride,
            "item_description": item,
            "rider_phone": contact_phone,
            "rider_name": display_rider if display_rider != "n/a" else None,
            "whatsapp_ok": whatsapp_ok,
            "whatsapp_error": whatsapp_error,
        }
        logger.info(
            "Queued Pipedrive ticket for after call ends (ride=%s wa_ok=%s)",
            ride["ride_id"],
            whatsapp_ok,
        )

        speak_parts: list[str] = []
        if whatsapp_ok:
            speak_parts.append(
                f"I have messaged the driver, {ride['driver_name']}, on WhatsApp "
                f"about your {item}."
            )
        else:
            speak_parts.append(
                "I could not reach the driver on WhatsApp just now."
            )
        speak_parts.append(
            "A support ticket will be created when this call ends. "
            "Thank you for calling GreenSM."
        )

        return {
            "ok": True,
            "pipedrive_deferred": True,
            "ticket_id": None,
            "whatsapp_ok": whatsapp_ok,
            "whatsapp_error": whatsapp_error,
            "driver_name": ride["driver_name"],
            "ride_id": ride["ride_id"],
            "agent_speak_summary": " ".join(speak_parts),
        }

    async def finalize_after_call(self) -> dict[str, Any] | None:
        """Create the Pipedrive ticket after the voice call hangs up."""
        pending = self._pending_after_call
        self._pending_after_call = None
        if not pending:
            return None

        ride = pending["ride"]
        item = pending["item_description"]
        try:
            ticket = self.pipedrive.create_lost_item_ticket(
                ride=ride,
                item_description=item,
                rider_phone=pending["rider_phone"],
                rider_name=pending.get("rider_name"),
            )
        except Exception:
            logger.exception("Post-call Pipedrive ticket failed")
            return {"ok": False, "ride_id": ride.get("ride_id")}

        deal_id = ticket.get("deal_id")
        logger.info(
            "Post-call Pipedrive ticket created deal_id=%s ride=%s",
            deal_id,
            ride.get("ride_id"),
        )

        try:
            self.registry.register(
                ride["driver_whatsapp"], deal_id, ride["ride_id"]
            )
        except Exception:
            logger.exception("Failed to register ticket for WA reply tracking")

        try:
            self.pipedrive.mark_whatsapp_status(
                deal_id,
                ok=bool(pending.get("whatsapp_ok")),
                driver_name=ride["driver_name"],
                driver_whatsapp=ride["driver_whatsapp"],
                item=item,
                error=pending.get("whatsapp_error"),
            )
        except Exception:
            logger.exception("Failed to log WhatsApp status on Pipedrive deal")

        return {"ok": True, "ticket_id": deal_id, "ride_id": ride.get("ride_id")}
