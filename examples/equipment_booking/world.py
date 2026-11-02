"""Resettable synthetic equipment-booking world (external example).

Mirrors RefundWorld semantics without touching core code: a reservation can
commit and then report a timeout; naive retry with a fresh idempotency key
creates a duplicate; stable-key retry does not.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any


WORLD_VERSION = "equipment-booking-world/1"


class ToolError(Exception):
    pass


class BookingTimeout(ToolError):
    pass


class BudgetExceeded(ToolError):
    pass


@dataclass(frozen=True)
class EquipmentFixture:
    equipment_id: str = "synthetic-microscope-1"
    user_id: str = "synthetic-user-1"
    slot: str = "2026-11-03T09:00Z"

    def __post_init__(self):
        for value in (self.equipment_id, self.user_id, self.slot):
            if not isinstance(value, str) or not value or len(value) > 100:
                raise ValueError("Invalid fixture identifier")


@dataclass(frozen=True)
class EquipmentScenario:
    timeout_after_commit: bool = False
    max_tool_calls: int = 20

    def __post_init__(self):
        if type(self.timeout_after_commit) is not bool:
            raise ValueError("timeout_after_commit must be boolean")
        if type(self.max_tool_calls) is not int or not 1 <= self.max_tool_calls <= 1000:
            raise ValueError("max_tool_calls must be between 1 and 1000")


class EquipmentWorld:
    """Authoritative reservations; one instance per baseline or faulted branch."""

    def __init__(
        self, fixture: EquipmentFixture, scenario: EquipmentScenario, *, actor_id: str | None = None
    ):
        self.fixture = fixture
        self.scenario = scenario
        self.actor_id = fixture.user_id if actor_id is None else actor_id
        if type(self.actor_id) is not str or not self.actor_id or len(self.actor_id) > 100:
            raise ValueError("Invalid actor_id")
        self.reservations: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self.calls = 0
        self._fault_fired = False

    def reset(self) -> None:
        """Clear synthetic state back to the immutable fixture."""
        self.reservations = []
        self.trace = []
        self.calls = 0
        self._fault_fired = False

    def snapshot(self):
        return {
            "equipment": asdict(self.fixture),
            "reservations": deepcopy(self.reservations),
            "total_bookings": len(self.reservations),
        }

    def call(self, tool: str, arguments: dict[str, Any]):
        if self.calls >= self.scenario.max_tool_calls:
            raise BudgetExceeded("Tool call budget exhausted")
        self.calls += 1
        request = {"tool": tool, "arguments": deepcopy(arguments)}
        try:
            result = self._dispatch(tool, arguments)
        except ToolError as error:
            self.trace.append(
                {
                    "event_id": self.calls,
                    "request": request,
                    "outcome": {"error": type(error).__name__, "message": str(error)},
                    "state": self.snapshot(),
                }
            )
            raise
        self.trace.append(
            {
                "event_id": self.calls,
                "request": request,
                "outcome": {"result": deepcopy(result)},
                "state": self.snapshot(),
            }
        )
        return deepcopy(result)

    def _dispatch(self, tool, arguments):
        schemas = {
            "get_equipment": {"equipment_id", "user_id"},
            "get_booking_status": {"equipment_id", "user_id", "idempotency_key"},
            "book_equipment": {"equipment_id", "user_id", "slot", "idempotency_key"},
        }
        if type(tool) is not str or tool not in schemas:
            raise ToolError("Unknown tool")
        if type(arguments) is not dict or set(arguments) != schemas[tool]:
            raise ToolError("Arguments do not match tool schema")
        for key, value in arguments.items():
            if not isinstance(value, str) or not value or len(value) > 100:
                raise ToolError("Invalid string argument")
        if self.actor_id != self.fixture.user_id or arguments["user_id"] != self.fixture.user_id:
            raise ToolError("User is not authorized for this equipment")
        if arguments["equipment_id"] != self.fixture.equipment_id:
            raise ToolError("Equipment does not exist")
        if tool == "get_equipment":
            return {**asdict(self.fixture), "available": True}
        existing = next(
            (
                row
                for row in self.reservations
                if row["idempotency_key"] == arguments["idempotency_key"]
            ),
            None,
        )
        if tool == "get_booking_status":
            return {"status": "committed" if existing else "not_found", "booking": deepcopy(existing)}
        if existing:
            if existing["slot"] != arguments["slot"]:
                raise ToolError("Idempotency key reused with different slot")
            return existing
        row = {
            "reservation_id": f"booking-{len(self.reservations) + 1}",
            "equipment_id": self.fixture.equipment_id,
            "user_id": arguments["user_id"],
            "slot": arguments["slot"],
            "idempotency_key": arguments["idempotency_key"],
        }
        self.reservations.append(row)
        if self.scenario.timeout_after_commit and not self._fault_fired:
            self._fault_fired = True
            raise BookingTimeout("Reservation committed; response timed out")
        return row
