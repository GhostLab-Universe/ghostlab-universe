"""Tiny seat/inventory reservation service (external example).

Stdlib-only, deterministic, in-process. No network, no secrets, no live calls,
no GhostLab imports. Intended as an INDEPENDENT world that an adapter drives.

Tools (allowlisted):
  - check_availability
  - reserve
  - cancel
  - get_status

Capacity rule: never commit beyond capacity (used + seats <= capacity),
unless the explicit ``skip_capacity_check`` fault is enabled.

Stale-read hazard: ``check_availability`` normally returns live state, but
when the ``stale_snapshot`` fault is enabled it returns the last cached
snapshot, which may be older than commit-time state.
"""
from __future__ import annotations

import copy

__all__ = [
    "ALLOWED_TOOLS",
    "FAULTS",
    "FIXTURES",
    "ReservationService",
    "TimeoutAfterCommit",
    "list_fixtures",
    "get_fixture",
]


class TimeoutAfterCommit(TimeoutError):
    """Raised AFTER a reserve commit when that fault is enabled."""


ALLOWED_TOOLS = (
    "check_availability",
    "reserve",
    "cancel",
    "get_status",
)

FAULTS = (
    "timeout_after_commit",
    "stale_snapshot",
    "skip_capacity_check",
)

# Named initial states: {capacity, reservations: [{holder, seats}]}.
# Reservation ids are assigned deterministically at reset: R-0001, ...
_FIXTURES = {
    "empty_small": {
        "capacity": 10,
        "reservations": [],
    },
    "half_full": {
        "capacity": 10,
        "reservations": [
            {"holder": "alice", "seats": 2},
            {"holder": "bob", "seats": 3},
        ],
    },
    "near_full": {
        "capacity": 6,
        "reservations": [
            {"holder": "alice", "seats": 5},
        ],
    },
    "full": {
        "capacity": 4,
        "reservations": [
            {"holder": "alice", "seats": 2},
            {"holder": "bob", "seats": 2},
        ],
    },
}

FIXTURES = copy.deepcopy(_FIXTURES)


def list_fixtures():
    """Return sorted fixture names."""
    return sorted(_FIXTURES)


def get_fixture(name):
    """Return a deep copy of the named fixture (never the live object)."""
    if name not in _FIXTURES:
        raise KeyError("unknown fixture: %r (known: %s)" % (name, sorted(_FIXTURES)))
    return copy.deepcopy(_FIXTURES[name])


def _ok(tool, data=None):
    return {"ok": True, "tool": tool, "data": data, "error": None}


def _err(tool, code, message):
    return {"ok": False, "tool": tool, "data": None,
            "error": {"code": code, "message": message}}
class ReservationService:
    """Deterministic in-process reservation world."""

    def __init__(self, fixture_name="empty_small"):
        self._closed = False
        self._faults = {name: False for name in FAULTS}
        self._snapshot_cache = None
        self._state = None
        self.reset(fixture_name)

    def reset(self, fixture_name="empty_small"):
        """Restore the exact named fixture. Reopens, clears faults/cache."""
        fixture = get_fixture(fixture_name)  # raises KeyError on unknown
        reservations = []
        next_id = 1
        for entry in fixture["reservations"]:
            rid = "R-%04d" % next_id
            next_id += 1
            reservations.append(
                {
                    "id": rid,
                    "holder": entry["holder"],
                    "seats": entry["seats"],
                    "status": "active",
                }
            )
        self._state = {
            "fixture": fixture_name,
            "capacity": int(fixture["capacity"]),
            "reservations": reservations,
            "revision": 0,
            "next_id": next_id,
        }
        self._faults = {name: False for name in FAULTS}
        self._snapshot_cache = None
        self._closed = False
        return _ok("reset", {"fixture": fixture_name, "state": self.observe()})

    def close(self):
        """Mark closed. Tool calls stop; observe() still works for evidence."""
        self._closed = True
        return _ok("close", {"closed": True})

    @property
    def closed(self):
        return self._closed

    def observe(self):
        """Return a deep copy of authoritative state (safe to mutate)."""
        capacity = self._state["capacity"]
        active = [r for r in self._state["reservations"] if r["status"] == "active"]
        used = sum(r["seats"] for r in active)
        return copy.deepcopy(
            {
                "fixture": self._state["fixture"],
                "capacity": capacity,
                "used": used,
                "available": capacity - used,
                "revision": self._state["revision"],
                "reservations": active,
                "all_reservations": list(self._state["reservations"]),
                "closed": self._closed,
                "faults": dict(self._faults),
            }
        )

    def check_invariants(self):
        """Detect violations purely from authoritative observed state."""
        seen = self.observe()
        violations = []
        if seen["used"] < 0:
            violations.append("negative_used")
        if seen["used"] > seen["capacity"]:
            violations.append("capacity_exceeded")
        recomputed = sum(r["seats"] for r in seen["reservations"])
        if recomputed != seen["used"]:
            violations.append("used_mismatch")
        for r in seen["all_reservations"]:
            if r["seats"] <= 0:
                violations.append("nonpositive_seats:%s" % r["id"])
            if r["status"] not in ("active", "cancelled"):
                violations.append("bad_status:%s" % r["id"])
        return {"ok": not violations, "violations": violations, "state": seen}

    def set_fault(self, name, enabled=True):
        """Explicit injection point. Faults are disabled by default."""
        if name not in self._faults:
            raise KeyError("unknown fault: %r (known: %s)"
                           % (name, sorted(self._faults)))
        self._faults[name] = bool(enabled)
        return {"fault": name, "enabled": bool(enabled)}

    def clear_faults(self):
        for name in self._faults:
            self._faults[name] = False
        return {"cleared": sorted(self._faults)}

    def get_faults(self):
        return dict(self._faults)
    def check_availability(self):
        if self._closed:
            return _err("check_availability", "closed", "service is closed")
        live = self.observe()
        payload_live = {
            "capacity": live["capacity"],
            "used": live["used"],
            "available": live["available"],
            "revision": live["revision"],
            "stale": False,
        }
        if self._faults["stale_snapshot"] and self._snapshot_cache is not None:
            stale = copy.deepcopy(self._snapshot_cache)
            stale["stale"] = True
            return _ok("check_availability", stale)
        self._snapshot_cache = copy.deepcopy(payload_live)
        return _ok("check_availability", copy.deepcopy(payload_live))

    def reserve(self, holder, seats):
        if self._closed:
            return _err("reserve", "closed", "service is closed")
        if not isinstance(holder, str) or not holder.strip():
            return _err("reserve", "invalid_args",
                        "holder must be a non-empty string")
        if not isinstance(seats, int) or isinstance(seats, bool) or seats <= 0:
            return _err("reserve", "invalid_args",
                        "seats must be a positive int")
        live = self.observe()
        if not self._faults["skip_capacity_check"]:
            if live["used"] + seats > live["capacity"]:
                return _err(
                    "reserve",
                    "capacity_exceeded",
                    "used %d + %d exceeds capacity %d"
                    % (live["used"], seats, live["capacity"]),
                )
        rid = "R-%04d" % self._state["next_id"]
        self._state["next_id"] += 1
        self._state["reservations"].append(
            {"id": rid, "holder": holder, "seats": seats, "status": "active"}
        )
        self._state["revision"] += 1
        after = self.observe()
        result = _ok(
            "reserve",
            {
                "reservation_id": rid,
                "holder": holder,
                "seats": seats,
                "used": after["used"],
                "available": after["available"],
                "revision": after["revision"],
            },
        )
        if self._faults["timeout_after_commit"]:
            raise TimeoutAfterCommit("injected timeout after commit of %s" % rid)
        return result

    def cancel(self, reservation_id):
        if self._closed:
            return _err("cancel", "closed", "service is closed")
        if not isinstance(reservation_id, str) or not reservation_id:
            return _err("cancel", "invalid_args",
                        "reservation_id must be a non-empty string")
        for record in self._state["reservations"]:
            if record["id"] == reservation_id:
                if record["status"] != "active":
                    return _err("cancel", "already_cancelled",
                                "%s already cancelled" % reservation_id)
                record["status"] = "cancelled"
                self._state["revision"] += 1
                after = self.observe()
                return _ok(
                    "cancel",
                    {
                        "reservation_id": reservation_id,
                        "used": after["used"],
                        "available": after["available"],
                        "revision": after["revision"],
                    },
                )
        return _err("cancel", "not_found",
                    "unknown reservation: %s" % reservation_id)

    def get_status(self, reservation_id):
        if self._closed:
            return _err("get_status", "closed", "service is closed")
        if not isinstance(reservation_id, str) or not reservation_id:
            return _err("get_status", "invalid_args",
                        "reservation_id must be a non-empty string")
        for record in self._state["reservations"]:
            if record["id"] == reservation_id:
                return _ok("get_status", copy.deepcopy(record))
        return _err("get_status", "not_found",
                    "unknown reservation: %s" % reservation_id)
    def call_tool(self, name, args=None):
        """Dispatch one allowlisted tool with strict arg validation."""
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _err(str(name), "invalid_args", "args must be a dict")
        if name not in ALLOWED_TOOLS:
            return _err(str(name), "unknown_tool", "unknown tool: %r" % (name,))
        specs = {
            "check_availability": set(),
            "reserve": {"holder", "seats"},
            "cancel": {"reservation_id"},
            "get_status": {"reservation_id"},
        }
        allowed = specs[name]
        unknown = sorted(set(args) - allowed)
        if unknown:
            return _err(name, "unknown_arg",
                        "unknown args for %s: %s" % (name, unknown))
        missing = sorted(allowed - set(args))
        if missing:
            return _err(name, "invalid_args",
                        "missing args for %s: %s" % (name, missing))
        if name == "check_availability":
            return self.check_availability()
        if name == "reserve":
            return self.reserve(args["holder"], args["seats"])
        if name == "cancel":
            return self.cancel(args["reservation_id"])
        return self.get_status(args["reservation_id"])