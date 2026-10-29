"""Atomic budget reservations for M05 (M04 extension to ghostlab-storage/1).

M05 decides what to reserve and M06 supplies the ceiling; this ledger only
guarantees that concurrent reservations never exceed it. Committed usage for a
campaign/resource is the sum of open reservations, plus reconciled actuals.
A reconciled reservation with unknown actual usage keeps counting its full
reserved amount, so unknown cost is never treated as zero.
"""

import sqlite3
from pathlib import Path
from typing import Protocol, runtime_checkable

from ghostlab.contracts import BudgetReservation

from . import sqlite as db
from .interfaces import Conflict, NotFound

BUDGET_INTERFACE_VERSION = "ghostlab-storage-budgets/1"


class BudgetExceeded(Conflict):
    """The reservation would take committed usage past the ceiling."""


@runtime_checkable
class BudgetLedger(Protocol):
    def reserve(self, reservation: BudgetReservation, *, limit: int) -> BudgetReservation: ...
    def reconcile(self, reservation_id: str, actual: int | None) -> BudgetReservation: ...
    def release(self, reservation_id: str) -> BudgetReservation: ...
    def committed(self, campaign_id: str, resource: str) -> tuple[int, int]: ...


_COMMITTED = ("SELECT COALESCE(SUM(CASE WHEN status = 'reserved' THEN amount "
              "WHEN status = 'reconciled' THEN COALESCE(actual, amount) ELSE 0 END), 0), "
              "COALESCE(SUM(status = 'reconciled' AND actual IS NULL), 0) "
              "FROM budget_reservations WHERE campaign_id = ? AND resource = ?")


class SqliteBudgetLedger:
    def __init__(self, path: Path):
        self.path = path

    def reserve(self, reservation: BudgetReservation, *, limit: int) -> BudgetReservation:
        if reservation.status != "reserved":
            raise Conflict("New reservations must have status 'reserved'")
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        try:
            with db.write(self.path) as conn:
                used, _ = conn.execute(_COMMITTED, (reservation.campaign_id,
                                                    reservation.resource)).fetchone()
                if used + reservation.amount > limit:
                    raise BudgetExceeded("Reservation exceeds the budget ceiling")
                conn.execute("INSERT INTO budget_reservations VALUES (?,?,?,?,?,NULL,?)",
                             (reservation.reservation_id, reservation.campaign_id,
                              reservation.resource, reservation.amount, reservation.status,
                              reservation.model_dump_json()))
        except sqlite3.IntegrityError as error:
            raise Conflict("Reservation id already exists") from error
        return reservation

    def reconcile(self, reservation_id: str, actual: int | None) -> BudgetReservation:
        return self._close(reservation_id, "reconciled", actual)

    def release(self, reservation_id: str) -> BudgetReservation:
        return self._close(reservation_id, "released", None)

    def committed(self, campaign_id: str, resource: str) -> tuple[int, int]:
        """(committed amount, reconciled reservations whose actual is unknown)."""
        with db.read(self.path) as conn:
            used, unknown = conn.execute(_COMMITTED, (campaign_id, resource)).fetchone()
        return int(used), int(unknown)

    def _close(self, reservation_id, status, actual) -> BudgetReservation:
        with db.write(self.path) as conn:
            row = conn.execute("SELECT body FROM budget_reservations WHERE reservation_id = ?",
                               (reservation_id,)).fetchone()
            if row is None:
                raise NotFound("Reservation not found")
            current = db.load(BudgetReservation, row[0])
            if current.status != "reserved":
                raise Conflict("Reservation is already closed")
            updated = db.revise(BudgetReservation, current, status=status, actual=actual)
            conn.execute("UPDATE budget_reservations SET status = ?, actual = ?, body = ? "
                         "WHERE reservation_id = ? AND status = 'reserved'",
                         (status, actual, updated.model_dump_json(), reservation_id))
        return updated
