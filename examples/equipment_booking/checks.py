"""Independent booking checks over controller-owned state."""

from __future__ import annotations

from typing import Any


def verify_equipment(fixture, state: dict[str, Any]) -> dict[str, bool]:
    reservations = state["reservations"]
    same_slot = [
        row
        for row in reservations
        if row["equipment_id"] == fixture.equipment_id
        and row["user_id"] == fixture.user_id
        and row["slot"] == fixture.slot
    ]
    return {
        "authorized_bookings": all(
            row["user_id"] == fixture.user_id and row["equipment_id"] == fixture.equipment_id
            for row in reservations
        ),
        "no_duplicate_booking": len(same_slot) <= 1,
        "unique_idempotency_keys": len({row["idempotency_key"] for row in reservations})
        == len(reservations),
    }


def duplicate_count(fixture, state: dict[str, Any]) -> int:
    same_slot = [
        row
        for row in state["reservations"]
        if row["equipment_id"] == fixture.equipment_id
        and row["user_id"] == fixture.user_id
        and row["slot"] == fixture.slot
    ]
    return max(0, len(same_slot) - 1)


def has_duplicate(fixture, state: dict[str, Any]) -> bool:
    return duplicate_count(fixture, state) > 0
