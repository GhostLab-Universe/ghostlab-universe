"""Normalized evidence for the P4 rule library: trace/state -> fact tables.

Builders are strict about top-level shapes (a trace list plus a state mapping)
and best-effort per fact table: a table that cannot be built is omitted and
listed in ``EvidenceView.missing``. Rules that require a missing table return
UNKNOWN, never pass. Standard library only.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

REFUND_WORLD = "refund-world/1"
JOURNEY_WORLD = "group-journey/1"


@dataclass(frozen=True)
class EvidenceView:
    """One world's normalized facts plus the tables that could not be built."""

    world_version: str
    facts: Mapping[str, Any]
    missing: frozenset[str]

    def __post_init__(self):
        if type(self.world_version) is not str or not self.world_version:
            raise ValueError("Invalid world version")
        if not isinstance(self.facts, Mapping):
            raise ValueError("Invalid facts")
        if not isinstance(self.missing, frozenset):
            raise ValueError("Invalid missing set")
        if set(self.facts) & set(self.missing):
            raise ValueError("A fact cannot be present and missing")


def _normalize_events(trace):
    """Normalized per-event rows, or None when the trace is malformed."""
    events = []
    for event in trace:
        if not isinstance(event, Mapping):
            return None
        event_id = event.get("event_id")
        request = event.get("request")
        outcome = event.get("outcome")
        if (type(event_id) is not int or not 1 <= event_id <= 1000
                or not isinstance(request, Mapping)
                or not isinstance(outcome, Mapping)):
            return None
        tool = request.get("tool")
        arguments = request.get("arguments")
        if type(tool) is not str or not tool or not isinstance(arguments, Mapping):
            return None
        if "result" in outcome and "error" in outcome:
            return None
        if "result" in outcome:
            if not isinstance(outcome["result"], Mapping):
                return None
            events.append({"event_id": event_id, "tool": tool,
                           "arguments": deepcopy(dict(arguments)), "ok": True,
                           "error": None, "result": deepcopy(dict(outcome["result"]))})
        elif "error" in outcome:
            if type(outcome["error"]) is not str or not outcome["error"]:
                return None
            events.append({"event_id": event_id, "tool": tool,
                           "arguments": deepcopy(dict(arguments)), "ok": False,
                           "error": outcome["error"], "result": None})
        else:
            return None
    return tuple(events)


def _refund_attribution(trace):
    """Commit event per ledger position: the ledger is append-only, so the
    event whose post-state ledger first reaches a position committed it.
    Positional attribution stays exact when a key commits twice."""
    pos_events: list[int] = []
    for event in trace:
        if not isinstance(event, Mapping):
            continue
        state = event.get("state")
        event_id = event.get("event_id")
        if not isinstance(state, Mapping) or type(event_id) is not int:
            continue
        ledger = state.get("ledger")
        if type(ledger) is not list:
            continue
        while len(pos_events) < len(ledger):
            pos_events.append(event_id)
    return pos_events


def _ledger_growth(trace):
    """Event ids at which the post-state ledger grew."""
    growth, size = [], 0
    for event in trace:
        if not isinstance(event, Mapping) or not isinstance(event.get("state"), Mapping):
            continue
        ledger = event["state"].get("ledger")
        if type(ledger) is not list:
            continue
        if len(ledger) > size:
            if type(event.get("event_id")) is int:
                growth.append(event["event_id"])
            size = len(ledger)
    return tuple(growth)


def refund_evidence(trace, state, *, actor_id=None):
    """Normalized facts for ``refund-world/1`` from a recorded trace and state.

    ``actor_id`` is the controller-bound principal from the run record; the
    world snapshot does not carry it, so a missing actor yields UNKNOWN for
    the actor rule rather than a guess. Ledger rows are attributed positionally to the
    trace event whose post-state ledger first reached them; a row no
    trace state ever showed falls back to the final event and is marked
    ``fallback-final`` in the row, so tampered states still fail loudly.
    """
    if type(trace) is not list or len(trace) > 1000:
        raise ValueError("Invalid refund trace")
    if not isinstance(state, Mapping):
        raise ValueError("Invalid refund state")
    if actor_id is not None and (type(actor_id) is not str or not actor_id):
        raise ValueError("Invalid actor id")
    facts, missing = {}, set()

    events = _normalize_events(trace)
    if events is None:
        missing.add("events")
    else:
        facts["events"] = events

    order = state.get("order")
    if (isinstance(order, Mapping) and type(order.get("order_id")) is str
            and order.get("order_id") and type(order.get("customer_id")) is str
            and order.get("customer_id") and type(order.get("paid_minor")) is int
            and order.get("paid_minor") > 0):
        facts["principals"] = {"order_id": order["order_id"],
                               "customer_id": order["customer_id"],
                               "paid_minor": order["paid_minor"],
                               "actor_id": actor_id}
    else:
        missing.add("principals")

    ledger, ledger_ok = [], True
    raw_ledger = state.get("ledger")
    if type(raw_ledger) is not list or len(raw_ledger) > 1000:
        ledger_ok = False
    else:
        for row in raw_ledger:
            if (not isinstance(row, Mapping)
                    or type(row.get("transaction_id")) is not str
                    or type(row.get("order_id")) is not str
                    or type(row.get("customer_id")) is not str
                    or type(row.get("amount_minor")) is not int
                    or type(row.get("idempotency_key")) is not str
                    or not row.get("idempotency_key")):
                ledger_ok = False
                break
            ledger.append({"transaction_id": row["transaction_id"],
                           "order_id": row["order_id"],
                           "customer_id": row["customer_id"],
                           "amount_minor": row["amount_minor"],
                           "idempotency_key": row["idempotency_key"]})
    if not ledger_ok:
        missing.add("ledger")
    else:
        origin = _refund_attribution(trace)
        last = facts["events"][-1]["event_id"] if facts.get("events") else None
        attributed = []
        for index, row in enumerate(ledger):
            if index < len(origin):
                event_id, how = origin[index], "traced"
            else:
                event_id, how = last, "fallback-final"
            if event_id is None:
                ledger_ok = False  # a commit with no citable event
                break
            attributed.append({**row, "event_id": event_id, "origin": how})
        if not ledger_ok:
            missing.add("ledger")
        else:
            facts["ledger"] = tuple(attributed)
            facts["ledger_growth"] = _ledger_growth(trace)

    if "events" in facts:
        reads, reads_ok = [], True
        for event in facts["events"]:
            if event["tool"] != "get_refund_status" or not event["ok"]:
                continue
            key = event["arguments"].get("idempotency_key")
            status = event["result"].get("status")
            if type(key) is not str or status not in ("committed", "not_found"):
                reads_ok = False
                break
            reads.append({"event_id": event["event_id"], "key": key, "status": status})
        if reads_ok:
            facts["status_reads"] = tuple(reads)
        else:
            missing.add("status_reads")
    else:
        missing.add("status_reads")

    if "ledger" in facts and "principals" in facts:
        facts["totals"] = {"total": sum(row["amount_minor"] for row in facts["ledger"]),
                           "paid": facts["principals"]["paid_minor"]}
    else:
        missing.add("totals")
    return EvidenceView(REFUND_WORLD, facts, frozenset(missing))
_JOURNEY_STATE_KEYS = ("approval_timeline", "permission_timeline", "commitment_log",
                      "delivery_log", "reservations", "calendar")


def _timeline_rows(rows, fields):
    """Validated timeline rows, or None when malformed."""
    if type(rows) is not list or not rows:
        return None
    found = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        if type(row.get("from_event")) is not int or row["from_event"] < 1:
            return None
        checked = {"from_event": row["from_event"]}
        for name, kind in fields:
            value = row.get(name)
            if kind == "version" and (type(value) is not int or value < 1):
                return None
            if kind == "name" and (type(value) is not str or not value):
                return None
            if kind == "flag" and type(value) is not bool:
                return None
            checked[name] = value
        found.append(checked)
    return tuple(found)


def _commitment_rows(rows):
    if type(rows) is not list:
        return None
    found = []
    for row in rows:
        if (not isinstance(row, Mapping) or type(row.get("event_id")) is not int
                or not 1 <= row["event_id"] <= 1000
                or row.get("kind") not in ("reservation", "calendar")
                or type(row.get("intent_version")) is not int):
            return None
        checked = {"event_id": row["event_id"], "kind": row["kind"],
                   "intent_version": row["intent_version"]}
        if row["kind"] == "reservation":
            if type(row.get("ref")) is not str or not row["ref"]:
                return None
            checked["ref"] = row["ref"]
        else:
            if (type(row.get("participant_id")) is not str or not row["participant_id"]
                    or type(row.get("entry_id")) is not str or not row["entry_id"]):
                return None
            checked["participant_id"] = row["participant_id"]
            checked["entry_id"] = row["entry_id"]
        found.append(checked)
    return tuple(found)


def _delivery_rows(rows):
    if type(rows) is not list:
        return None
    found = []
    for row in rows:
        if (not isinstance(row, Mapping) or type(row.get("event_id")) is not int
                or not 1 <= row["event_id"] <= 1000
                or type(row.get("message_id")) is not str or not row["message_id"]
                or type(row.get("recipient_id")) is not str or not row["recipient_id"]):
            return None
        found.append({"event_id": row["event_id"], "message_id": row["message_id"],
                      "recipient_id": row["recipient_id"]})
    return tuple(found)


def _reservation_map(raw):
    if not isinstance(raw, Mapping):
        return None
    found = {}
    for hold_id, row in raw.items():
        if (type(hold_id) is not str or not isinstance(row, Mapping)
                or type(row.get("reservation_id")) is not str
                or row.get("status") not in ("confirmed", "cancelled")
                or type(row.get("confirmed_at")) is not int
                or not 1 <= row["confirmed_at"] <= 1000
                or (row.get("cancelled_at") is not None
                    and (type(row.get("cancelled_at")) is not int
                         or not 1 <= row["cancelled_at"] <= 1000))
                or type(row.get("intent_version")) is not int
                or type(row.get("option_id")) is not str
                or type(row.get("travellers")) is not list
                or any(type(t) is not str for t in row["travellers"])):
            return None
        found[hold_id] = {"reservation_id": row["reservation_id"], "status": row["status"],
                          "confirmed_at": row["confirmed_at"],
                          "cancelled_at": row.get("cancelled_at"),
                          "intent_version": row["intent_version"],
                          "option_id": row["option_id"],
                          "travellers": tuple(row["travellers"])}
    return found


def _hold_map(raw):
    if not isinstance(raw, Mapping):
        return None
    found = {}
    for hold_id, row in raw.items():
        if (type(hold_id) is not str or not isinstance(row, Mapping)
                or type(row.get("option_id")) is not str
                or row.get("status") not in ("held", "confirmed", "released")
                or type(row.get("held_at")) is not int
                or not 1 <= row["held_at"] <= 1000):
            return None
        found[hold_id] = {"option_id": row["option_id"], "status": row["status"],
                          "held_at": row["held_at"]}
    return found


def _calendar_map(raw):
    if not isinstance(raw, Mapping):
        return None
    found = {}
    for participant, entries in raw.items():
        if type(participant) is not str or type(entries) is not list:
            return None
        checked_entries = []
        for entry in entries:
            window = entry.get("window") if isinstance(entry, Mapping) else None
            if (not isinstance(entry, Mapping) or type(entry.get("entry_id")) is not str
                    or type(window) is not list or len(window) != 2
                    or any(type(v) is not int for v in window)
                    or (entry.get("reservation_id") is not None
                        and type(entry.get("reservation_id")) is not str)):
                return None
            checked_entries.append({"entry_id": entry["entry_id"],
                                    "window": (window[0], window[1]),
                                    "reservation_id": entry.get("reservation_id")})
        found[participant] = tuple(checked_entries)
    return found


def _message_map(raw):
    if not isinstance(raw, Mapping):
        return None
    found = {}
    for message_id, row in raw.items():
        if (type(message_id) is not str or not isinstance(row, Mapping)
                or type(row.get("recipient_id")) is not str
                or row.get("status") not in ("queued", "delivered")):
            return None
        delivered_at = row.get("delivered_at")
        if delivered_at is not None and (type(delivered_at) is not int
                                         or not 1 <= delivered_at <= 1000):
            return None
        found[message_id] = {"recipient_id": row["recipient_id"], "status": row["status"],
                             "delivered_at": delivered_at}
    return found


def _trace_holds(trace):
    """Per-event active holds from post-state snapshots, or None if any lack them."""
    history = []
    for event in trace:
        if not isinstance(event, Mapping) or not isinstance(event.get("state"), Mapping):
            return None
        holds = _hold_map(event["state"].get("holds"))
        if holds is None:
            return None
        event_id = event.get("event_id")
        if type(event_id) is not int:
            return None
        history.append((event_id, holds))
    return tuple(history)


def journey_evidence(trace, state, *, fixture=None):
    """Normalized facts for ``group-journey/1`` from a trace, state and fixture.

    The world snapshot carries timelines and logs but no fixture, so the
    caller supplies the :class:`JourneyFixture` the run started from; a
    missing fixture yields UNKNOWN for every fixture-dependent rule.
    """
    if type(trace) is not list or len(trace) > 1000:
        raise ValueError("Invalid journey trace")
    if not isinstance(state, Mapping):
        raise ValueError("Invalid journey state")
    if fixture is not None and type(fixture).__name__ != "JourneyFixture":
        raise ValueError("Invalid journey fixture")
    facts, missing = {}, set()

    events = _normalize_events(trace)
    if events is None:
        missing.add("events")
    else:
        facts["events"] = events

    if type(state.get("journey_id")) is str and state["journey_id"]:
        facts["journey_id"] = state["journey_id"]
    else:
        missing.add("journey_id")

    if fixture is None:
        missing.update(("journey_fixture", "participants", "options"))
    else:
        facts["journey_fixture"] = fixture
        facts["participants"] = {"organizer": fixture.organizer,
                                 "mandatory": tuple(fixture.mandatory),
                                 "viewers": tuple(fixture.viewers),
                                 "all": frozenset(fixture.participants)}
        facts["options"] = {o.option_id: {"depart": o.depart, "arrive": o.arrive,
                                          "seats": o.seats} for o in fixture.options}

    if all(k in state for k in _JOURNEY_STATE_KEYS):
        facts["journey_state"] = state
    else:
        missing.add("journey_state")

    tables = {
        "approval_timeline": _timeline_rows(state.get("approval_timeline"),
                                           (("version", "version"),)),
        "permission_timeline": _timeline_rows(state.get("permission_timeline"),
                                             (("participant", "name"), ("sharing", "flag"))),
        "commitment_log": _commitment_rows(state.get("commitment_log")),
        "delivery_log": _delivery_rows(state.get("delivery_log")),
        "reservations": _reservation_map(state.get("reservations")),
        "holds": _hold_map(state.get("holds")),
        "calendar": _calendar_map(state.get("calendar")),
        "messages": _message_map(state.get("messages")),
        "trace_holds": _trace_holds(trace),
    }
    for name, table in tables.items():
        if table is None:
            missing.add(name)
        else:
            facts[name] = table
    return EvidenceView(JOURNEY_WORLD, facts, frozenset(missing))
