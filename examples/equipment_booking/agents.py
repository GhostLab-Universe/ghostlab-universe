"""Naive vs corrected booking agents. Both route via the gateway."""

from __future__ import annotations

from typing import Any

from .world import BookingTimeout


def run_naive(gateway, run_id: str, world, fixture) -> dict[str, Any]:
    """Vulnerable: retries a timed-out booking with a FRESH idempotency key."""
    identity = {"equipment_id": fixture.equipment_id, "user_id": fixture.user_id}
    gateway.dispatch(run_id, world, "get_equipment", dict(identity))
    request = {**identity, "slot": fixture.slot, "idempotency_key": "booking-intent-1"}
    try:
        gateway.dispatch(run_id, world, "book_equipment", dict(request))
    except BookingTimeout:
        gateway.dispatch(
            run_id, world, "book_equipment", {**request, "idempotency_key": "booking-retry-2"}
        )
    return world.snapshot()


def run_corrected(gateway, run_id: str, world, fixture) -> dict[str, Any]:
    """Repaired: on timeout, checks status with the STABLE key before retrying."""
    identity = {"equipment_id": fixture.equipment_id, "user_id": fixture.user_id}
    gateway.dispatch(run_id, world, "get_equipment", dict(identity))
    request = {**identity, "slot": fixture.slot, "idempotency_key": "booking-intent-1"}
    try:
        gateway.dispatch(run_id, world, "book_equipment", dict(request))
    except BookingTimeout:
        status = gateway.dispatch(
            run_id,
            world,
            "get_booking_status",
            {**identity, "idempotency_key": request["idempotency_key"]},
        )
        if status["status"] != "committed":
            gateway.dispatch(run_id, world, "book_equipment", dict(request))
    return world.snapshot()
