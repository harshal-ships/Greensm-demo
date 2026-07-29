"""Nova tool schemas and handlers — deterministic side effects (not the model)."""

from __future__ import annotations

import json
import logging
from typing import Any

from pipedrive import COMPLAINT_LABELS, PipedriveClient
from rides import lookup_rides, normalize_ride_id, ride_summary

logger = logging.getLogger(__name__)

RIDER_COMPLAINT_CATEGORIES = (
    "rude_driver",
    "unsafe_driving",
    "dirty_vehicle",
    "bad_route",
    "refused_stop",
)
DRIVER_COMPLAINT_CATEGORIES = ("abusive_rider", "vehicle_damage")

COMPLAINT_CATEGORIES_BY_ROLE = {
    "rider": RIDER_COMPLAINT_CATEGORIES,
    "driver": DRIVER_COMPLAINT_CATEGORIES,
}


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
                "Look up a recent GreenSM ride for a rider or driver reporting a trip. "
                "Use after the caller describes pickup, dropoff, or ride id. "
                "Ride ids look like G88421 (spoken as G eight eight four two one)."
            ),
            "inputSchema": {
                "json": json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "ride_id": {
                                "type": "string",
                                "description": "GreenSM ride id like G88421",
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
            "name": "registerCase",
            "description": (
                "After the caller confirms the ride and details, queue a support case. "
                "Pipedrive ticket is created when the call ends — do not invent a ticket "
                "number. Do not ask for a callback number."
            ),
            "inputSchema": {
                "json": json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "case_type": {
                                "type": "string",
                                "enum": ["lost", "complaint"],
                                "description": "lost or complaint",
                            },
                            "caller_role": {
                                "type": "string",
                                "enum": ["rider", "driver"],
                                "description": "Who is calling",
                            },
                            "ride_id": {
                                "type": "string",
                                "description": "Confirmed GreenSM ride id",
                            },
                            "item_description": {
                                "type": "string",
                                "description": "Required for lost cases (riders only)",
                            },
                            "complaint_category": {
                                "type": "string",
                                "enum": list(
                                    RIDER_COMPLAINT_CATEGORIES
                                    + DRIVER_COMPLAINT_CATEGORIES
                                ),
                                "description": "Required for complaint cases",
                            },
                            "description": {
                                "type": "string",
                                "description": "Short summary of the complaint",
                            },
                            "caller_name": {
                                "type": "string",
                                "description": "Caller name if known",
                            },
                        },
                        "required": [
                            "case_type",
                            "caller_role",
                            "ride_id",
                        ],
                    }
                )
            },
        }
    },
]


class SupportTools:
    """Per-call tool surface: lookup rides, queue case for Pipedrive on hangup."""

    def __init__(
        self,
        pipedrive: PipedriveClient,
        *,
        caller_phone: str = "",
    ):
        self.pipedrive = pipedrive
        self.caller_phone = (caller_phone or "").strip()
        self._pending_after_call: dict[str, Any] | None = None

    async def execute(self, tool_name: str, tool_content: dict) -> dict[str, Any]:
        try:
            args = _parse_tool_args(tool_content)
            logger.info("Tool %s args=%s", tool_name, args)

            if tool_name == "lookupRecentRide":
                return self._lookup(args)
            if tool_name == "registerCase":
                return self._register_case(args)
            return {
                "ok": False,
                "error": f"Unknown tool: {tool_name}",
                "agent_speak_summary": (
                    "Something went wrong. Please say your ride details again."
                ),
            }
        except Exception as exc:
            logger.exception("Unhandled tool error in %s", tool_name)
            return {
                "ok": False,
                "error": str(exc),
                "agent_speak_summary": (
                    "I hit a technical issue. Please repeat your details and I will try again."
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
                    + " Ask for pickup, dropoff, or the ride id again as G plus the numbers."
                ),
                "agent_speak_summary": (
                    "I could not find that ride. Please tell me the pickup and dropoff, "
                    "or say the ride id as G, then the numbers."
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
            "message": "Confirm the correct ride with the caller before registering the case.",
            "agent_speak_summary": speak,
        }

    def _register_case(self, args: dict) -> dict[str, Any]:
        case_type = (args.get("case_type") or "").strip().lower()
        caller_role = (args.get("caller_role") or "").strip().lower()
        ride_id = normalize_ride_id(args.get("ride_id")) or (
            (args.get("ride_id") or "").strip().upper()
        )
        caller_name = (args.get("caller_name") or "").strip() or None

        if case_type not in ("lost", "complaint"):
            return {
                "ok": False,
                "error": "case_type must be lost or complaint",
                "agent_speak_summary": "Is this a lost item or a complaint?",
            }
        if caller_role not in ("rider", "driver"):
            return {
                "ok": False,
                "error": "caller_role must be rider or driver",
                "agent_speak_summary": "Are you calling as a rider or a driver?",
            }
        if not ride_id:
            return {
                "ok": False,
                "error": "ride_id is required",
                "agent_speak_summary": "I still need the ride id before I can register this.",
            }

        if case_type == "lost" and caller_role == "driver":
            return {
                "ok": False,
                "error": "Drivers cannot file lost-item cases",
                "agent_speak_summary": (
                    "Lost items are filed by riders. "
                    "If you need to report something else, please describe it as a complaint."
                ),
            }

        item_description = (args.get("item_description") or "").strip()
        complaint_category = (args.get("complaint_category") or "").strip()
        description = (args.get("description") or "").strip()

        if case_type == "lost":
            if not item_description:
                return {
                    "ok": False,
                    "error": "item_description required for lost cases",
                    "agent_speak_summary": "What did you leave in the car?",
                }
        else:
            allowed = COMPLAINT_CATEGORIES_BY_ROLE.get(caller_role, ())
            if complaint_category not in allowed:
                return {
                    "ok": False,
                    "error": f"Invalid complaint_category for {caller_role}",
                    "agent_speak_summary": (
                        "Please describe the complaint so I can categorize it correctly."
                    ),
                }
            if not description:
                return {
                    "ok": False,
                    "error": "description required for complaints",
                    "agent_speak_summary": "Please briefly describe what happened.",
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

        self._pending_after_call = {
            "case_type": case_type,
            "caller_role": caller_role,
            "ride": ride,
            "caller_phone": contact_phone,
            "caller_name": caller_name,
            "item_description": item_description or None,
            "complaint_category": complaint_category or None,
            "description": description or None,
        }
        logger.info(
            "Queued Pipedrive case for after call (type=%s role=%s ride=%s)",
            case_type,
            caller_role,
            ride["ride_id"],
        )

        if case_type == "lost":
            speak = (
                f"I've registered your lost {item_description} for ride {ride['ride_id']}. "
                "A support ticket will be created when this call ends. "
                "Thank you for calling GreenSM Resolve."
            )
        else:
            label = COMPLAINT_LABELS.get(complaint_category, complaint_category)
            speak = (
                f"I've registered your complaint about {label} for ride {ride['ride_id']}. "
                "A support ticket will be created when this call ends. "
                "Thank you for calling GreenSM Resolve."
            )

        return {
            "ok": True,
            "pipedrive_deferred": True,
            "ticket_id": None,
            "case_type": case_type,
            "caller_role": caller_role,
            "ride_id": ride["ride_id"],
            "agent_speak_summary": speak,
        }

    async def finalize_after_call(self) -> dict[str, Any] | None:
        """Create the Pipedrive ticket after the voice call hangs up."""
        pending = self._pending_after_call
        self._pending_after_call = None
        if not pending:
            return None

        ride = pending["ride"]
        case_type = pending["case_type"]
        try:
            ticket = self.pipedrive.create_case(
                case_type=case_type,
                caller_role=pending["caller_role"],
                ride=ride,
                caller_phone=pending["caller_phone"],
                caller_name=pending.get("caller_name"),
                item_description=pending.get("item_description"),
                complaint_category=pending.get("complaint_category"),
                description=pending.get("description"),
            )
        except Exception:
            logger.exception("Post-call Pipedrive ticket failed")
            return {
                "ok": False,
                "case_type": case_type,
                "caller_role": pending.get("caller_role"),
                "ride_id": ride.get("ride_id"),
            }

        deal_id = ticket.get("deal_id")
        logger.info(
            "Post-call Pipedrive ticket created deal_id=%s case_type=%s role=%s ride=%s",
            deal_id,
            case_type,
            pending.get("caller_role"),
            ride.get("ride_id"),
        )

        return {
            "ok": True,
            "ticket_id": deal_id,
            "case_type": case_type,
            "caller_role": pending.get("caller_role"),
            "ride_id": ride.get("ride_id"),
        }
