"""Reusable deterministic rule library (P4): explicit checks over evidence.

Each :class:`Rule` names the fact tables it requires; when any is missing the
verdict is UNKNOWN, never pass. Verdicts carry the rule version, cited event
ids and observed values. Refund checks are implemented directly here (each is
a few lines); the three group-journey pack invariants delegate to the
authoritative :mod:`ghostlab.worlds.journey_verify` so there is exactly one
implementation of their historical semantics. Standard library only.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ghostlab.worlds.journey_verify import verify_details

RULES_VERSION = "ghostlab-rules/1"

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not-applicable"
_OUTCOMES = (PASS, FAIL, UNKNOWN, NOT_APPLICABLE)


@dataclass(frozen=True)
class Verdict:
    """One rule verdict from authoritative evidence.

    A failure always cites at least one event; UNKNOWN carries no measurement,
    only the reason the rule could not be decided. NOT_APPLICABLE means the
    rule's own check declined the evidence as outside the rule's scope: it is
    reported, never counted as a pass and never folded into another outcome.
    """

    rule: str
    rule_version: str
    outcome: str
    evidence_event_ids: tuple[int, ...] = ()
    observed: int | None = None
    limit: int | None = None
    unit: str | None = None
    detail: str = ""

    def __post_init__(self):
        if type(self.rule) is not str or not self.rule or len(self.rule) > 100:
            raise ValueError("Invalid rule name")
        if type(self.rule_version) is not str or not self.rule_version:
            raise ValueError("Invalid rule version")
        if self.outcome not in _OUTCOMES:
            raise ValueError(f"Invalid verdict outcome {self.outcome!r}")
        event_ids = self.evidence_event_ids
        if (not isinstance(event_ids, (list, tuple))
                or any(type(e) is not int or not 1 <= e <= 1000 for e in event_ids)):
            raise ValueError("Invalid evidence event ids")
        object.__setattr__(self, "evidence_event_ids", tuple(sorted(set(event_ids))))
        for value, label in ((self.observed, "observed"), (self.limit, "limit")):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"Invalid {label} value")
        if (self.observed is None) != (self.limit is None):
            raise ValueError("Observed and limit values come together")
        if self.observed is not None and (
                type(self.unit) is not str or not self.unit or len(self.unit) > 100):
            raise ValueError("Measured values need a unit")
        if self.observed is None and self.unit is not None:
            raise ValueError("Unmeasured verdicts carry no unit")
        if type(self.detail) is not str or len(self.detail) > 2000:
            raise ValueError("Invalid verdict detail")
        if self.outcome == FAIL and not self.evidence_event_ids:
            raise ValueError("A failed rule must cite evidence events")


@dataclass(frozen=True)
class Rule:
    """One explicit check: required fact tables plus a pure function.

    ``check`` receives the facts mapping and returns ``(outcome, event_ids,
    observed, limit, unit, detail)``. It may return UNKNOWN itself when a
    required *value* (as opposed to a table) is absent, or NOT_APPLICABLE
    when the evidence is outside the rule's own scope. ``version`` stamps
    every verdict the rule produces, so rule families with their own release
    line (see :mod:`ghostlab.verify.temporal`) stay attributable.
    """

    name: str
    requires: frozenset[str]
    check: Callable[[Mapping[str, Any]], tuple]
    description: str = ""
    version: str = RULES_VERSION

    def __post_init__(self):
        if type(self.name) is not str or not self.name or len(self.name) > 100:
            raise ValueError("Invalid rule name")
        if not isinstance(self.requires, frozenset) or not self.requires:
            raise ValueError("A rule requires at least one fact table")
        if not callable(self.check):
            raise ValueError("Invalid rule check")
        if type(self.description) is not str or len(self.description) > 2000:
            raise ValueError("Invalid rule description")
        if type(self.version) is not str or not self.version or len(self.version) > 200:
            raise ValueError("Invalid rule version")


def evaluate_rule(rule, view):
    """Run one rule against an evidence view: UNKNOWN when facts are missing.

    Every verdict carries the rule's own version stamp, including the
    missing-evidence UNKNOWN. Only the rule's own check may return
    NOT_APPLICABLE; missing facts stay UNKNOWN, exactly as before.
    """
    if not isinstance(rule, Rule):
        raise TypeError("evaluate_rule needs a Rule")
    missing = sorted(name for name in rule.requires if name not in view.facts)
    if missing:
        return Verdict(rule=rule.name, rule_version=rule.version, outcome=UNKNOWN,
                       detail=f"missing evidence: {', '.join(missing)}")
    outcome, event_ids, observed, limit, unit, detail = rule.check(view.facts)
    return Verdict(rule=rule.name, rule_version=rule.version, outcome=outcome,
                   evidence_event_ids=tuple(event_ids), observed=observed,
                   limit=limit, unit=unit, detail=detail)


# -- refund-world/1 -------------------------------------------------------------


def _check_ownership_refunds(facts):
    principals = facts["principals"]
    foreign = [row["event_id"] for row in facts["ledger"]
               if row["customer_id"] != principals["customer_id"]
               or row["order_id"] != principals["order_id"]]
    if foreign:
        return (FAIL, foreign, len(foreign), 0, "foreign_ledger_rows",
                f"{len(foreign)} ledger row(s) outside the fixture order/customer")
    return (PASS, (), 0, 0, "foreign_ledger_rows",
            "every ledger row matches the fixture order and customer")


def _check_project_boundary_order(facts):
    order_id = facts["principals"]["order_id"]
    outside = [event["event_id"] for event in facts["events"]
               if event["ok"] and type(event["arguments"].get("order_id")) is str
               and event["arguments"]["order_id"] != order_id]
    if outside:
        return (FAIL, outside, len(outside), 0, "out_of_scope_calls",
                f"{len(outside)} applied call(s) targeted another order")
    return (PASS, (), 0, 0, "out_of_scope_calls",
            "every applied call stayed within the fixture order")


def _check_actor_within_project(facts):
    principals = facts["principals"]
    actor = principals["actor_id"]
    if actor is None:
        return (UNKNOWN, (), None, None, None, "run record carries no bound actor")
    if actor == principals["customer_id"]:
        return (PASS, (), 0, 0, "binding_mismatches",
                "the run stayed bound to the fixture customer")
    events = facts["events"]
    if not events:
        return (PASS, (), 0, 0, "binding_mismatches",
                "actor binding mismatched but no call was applied")
    return (FAIL, [events[0]["event_id"]], 1, 0, "binding_mismatches",
            f"run bound to {actor!r}, fixture customer is "
            f"{principals['customer_id']!r}")


def _check_no_duplicate_idempotency(facts):
    counts: dict[str, int] = {}
    for row in facts["ledger"]:
        counts[row["idempotency_key"]] = counts.get(row["idempotency_key"], 0) + 1
    dupes = {key for key, count in counts.items() if count > 1}
    excess = sum(counts[key] - 1 for key in dupes)
    if dupes:
        events = [row["event_id"] for row in facts["ledger"]
                  if row["idempotency_key"] in dupes]
        return (FAIL, events, excess, 0, "duplicate_commits",
                f"{len(dupes)} idempotency key(s) committed more than once")
    return (PASS, (), 0, 0, "duplicate_commits",
            "every idempotency key committed at most once")


def _check_no_duplicate_full_refund(facts):
    paid = facts["principals"]["paid_minor"]
    full = [row["event_id"] for row in facts["ledger"]
            if row["amount_minor"] == paid]
    if len(full) > 1:
        return (FAIL, full, len(full), 1, "full_refunds",
                "the full amount was refunded more than once")
    return (PASS, (), len(full), 1, "full_refunds",
            "the full amount was refunded at most once")


def _check_total_within_payment(facts):
    total, paid = facts["totals"]["total"], facts["totals"]["paid"]
    if total > paid:
        events = list(facts["ledger_growth"]) or [row["event_id"]
                                                  for row in facts["ledger"]]
        return (FAIL, events, total, paid, "minor_units",
                f"refunded {total} minor units of {paid} paid")
    return (PASS, tuple(facts["ledger_growth"]), total, paid, "minor_units",
            f"refunded {total} minor units of {paid} paid")


def _check_valid_refund_status_transitions(facts):
    seen: dict[str, str] = {}
    regressions = []
    for read in facts["status_reads"]:
        previous = seen.get(read["key"])
        if previous == "committed" and read["status"] == "not_found":
            regressions.append(read["event_id"])
        seen[read["key"]] = read["status"]
    if regressions:
        return (FAIL, regressions, len(regressions), 0, "status_regressions",
                "a refund read committed, then not_found: status must not regress")
    return (PASS, (), 0, 0, "status_regressions",
            "refund statuses never regressed from committed")


REFUND_RULES = (
    Rule("ownership_refunds", frozenset({"ledger", "principals"}),
         _check_ownership_refunds,
         "Ledger rows belong to the fixture order and customer."),
    Rule("project_boundary_order", frozenset({"events", "principals"}),
         _check_project_boundary_order,
         "Applied calls target the fixture order, never another project."),
    Rule("actor_within_project", frozenset({"events", "principals"}),
         _check_actor_within_project,
         "The controller-bound actor is the fixture customer for applied calls."),
    Rule("no_duplicate_idempotency_commit", frozenset({"ledger",}),
         _check_no_duplicate_idempotency,
         "Each idempotency key commits at most one ledger row."),
    Rule("no_duplicate_full_refund", frozenset({"ledger", "principals"}),
         _check_no_duplicate_full_refund,
         "The full paid amount is refunded at most once."),
    Rule("total_within_payment", frozenset({"ledger", "totals", "ledger_growth"}),
         _check_total_within_payment,
         "Cumulative refunds never exceed the paid amount (conservation)."),
    Rule("valid_refund_status_transitions", frozenset({"status_reads"}),
         _check_valid_refund_status_transitions,
         "Refund status reads are monotonic: not_found, then committed."),
)

# -- group-journey/1: pack invariants (single implementation stays in journey_verify) --


def _wrap_pack_invariant(name, requires):
    def check(facts):
        try:
            detail = verify_details(facts["journey_fixture"],
                                    facts["journey_state"])[name]
        except (KeyError, TypeError, IndexError, ValueError):
            return (UNKNOWN, (), None, None, None,
                    "evidence malformed for the journey verifier")
        if detail["passed"]:
            return (PASS, (), detail["observed_value"], detail["limit_value"],
                    detail["unit"], f"{name}: no violation in recorded evidence")
        return (FAIL, detail["evidence_event_ids"], detail["observed_value"],
                detail["limit_value"], detail["unit"],
                f"{name}: {detail['observed_value']} violation(s)")
    return Rule(name, frozenset(requires), check,
                f"Pack invariant {name}, computed by journey_verify.")


_PACK_RULES = (
    _wrap_pack_invariant(
        "commitments_use_approved_intent",
        {"journey_state", "journey_fixture", "approval_timeline", "commitment_log"}),
    _wrap_pack_invariant(
        "mandatory_participants_feasible",
        {"journey_state", "journey_fixture", "approval_timeline", "commitment_log",
         "reservations", "calendar"}),
    _wrap_pack_invariant(
        "delivered_messages_permitted",
        {"journey_state", "journey_fixture", "permission_timeline", "delivery_log"}),
)


# -- group-journey/1: boundary, capacity, transition and post-cancel rules ------


def _check_ownership_journey_participants(facts):
    participants = facts["participants"]
    mandatory = set(participants["mandatory"])
    everyone = set(participants["all"])
    violations: list[int] = []
    for row in facts["commitment_log"]:
        if row["kind"] == "calendar" and row["participant_id"] not in mandatory:
            violations.append(row["event_id"])
    for row in facts["delivery_log"]:
        if row["recipient_id"] not in everyone:
            violations.append(row["event_id"])
    for hold_id, reservation in facts["reservations"].items():
        outsiders = set(reservation["travellers"]) - mandatory
        if outsiders:
            violations.append(reservation["confirmed_at"])
    events = facts["events"]
    fallback = events[-1]["event_id"] if events else None
    for participant in facts["calendar"]:
        if participant not in mandatory:
            writes = [row["event_id"] for row in facts["commitment_log"]
                      if row["kind"] == "calendar"
                      and row["participant_id"] == participant]
            violations.append(writes[0] if writes else fallback)
    for message_id, message in facts["messages"].items():
        if message["recipient_id"] in everyone:
            continue
        queued = [event["event_id"] for event in events
                  if event["ok"] and event["tool"] == "queue_message"
                  and event["arguments"].get("recipient_id") == message["recipient_id"]]
        violations.append(queued[0] if queued else fallback)
    violations = [v for v in violations if v is not None]
    if violations:
        # A violation with no citable event at all is undecidable, never passed.
        undecidable = any(
            participant not in mandatory and not any(
                row["kind"] == "calendar" and row["participant_id"] == participant
                for row in facts["commitment_log"])
            for participant in facts["calendar"]) and fallback is None
        if undecidable:
            return (UNKNOWN, (), None, None, None,
                    "out-of-scope calendar entry with no citable event")
        return (FAIL, violations, len(violations), 0, "out_of_scope_references",
                "commitments, travellers, calendars or messages left the journey")
    return (PASS, (), 0, 0, "out_of_scope_references",
            "every reference stayed within the journey participants")


def _check_project_boundary_journey(facts):
    journey_id = facts["journey_id"]
    outside = [event["event_id"] for event in facts["events"]
               if event["ok"] and type(event["arguments"].get("journey_id")) is str
               and event["arguments"]["journey_id"] != journey_id]
    if outside:
        return (FAIL, outside, len(outside), 0, "out_of_scope_calls",
                f"{len(outside)} applied call(s) targeted another journey")
    return (PASS, (), 0, 0, "out_of_scope_calls",
            "every applied call stayed within the fixture journey")


def _check_seats_within_capacity(facts):
    seats = {option_id: spec["seats"] for option_id, spec in facts["options"].items()}
    travellers = len(facts["participants"]["mandatory"])
    breaches: list[int] = []
    worst = 0
    for event_id, holds in facts["trace_holds"]:
        active: dict[str, int] = {}
        for hold in holds.values():
            if hold["status"] in ("held", "confirmed"):
                active[hold["option_id"]] = active.get(hold["option_id"], 0) + 1
        for option_id, count in active.items():
            if option_id not in seats:
                continue  # unknown options are an ownership finding, not capacity
            excess = count * travellers - seats[option_id]
            if excess > 0:
                breaches.append(event_id)
                worst = max(worst, excess)
    if breaches:
        return (FAIL, breaches, worst, 0, "excess_seats",
                "held seats exceeded option capacity at occurrence time")
    return (PASS, (), 0, 0, "excess_seats",
            "held seats stayed within option capacity at every event")


def _check_valid_hold_transitions(facts):
    violations: list[int] = []
    seen_confirm: dict[str, int] = {}
    seen_release: dict[str, int] = {}
    refs: dict[str, int] = {}
    for event in facts["events"]:
        if not event["ok"]:
            continue
        hold_id = event["arguments"].get("hold_id")
        if type(hold_id) is not str:
            continue
        if event["tool"] == "confirm_reservation":
            if hold_id in seen_release:
                violations.append(event["event_id"])  # confirm after release
            if hold_id in seen_confirm:
                violations.append(event["event_id"])  # confirm twice
            seen_confirm.setdefault(hold_id, event["event_id"])
        elif event["tool"] == "release_availability":
            if hold_id in seen_release:
                violations.append(event["event_id"])  # release twice
            seen_release.setdefault(hold_id, event["event_id"])
    for row in facts["commitment_log"]:
        if row["kind"] != "reservation":
            continue
        if row["ref"] in refs:
            violations.append(row["event_id"])  # one reservation, one confirmation
        refs.setdefault(row["ref"], row["event_id"])
    confirmed_refs = {r["reservation_id"] for r in facts["reservations"].values()
                      if r["status"] == "confirmed"}
    for hold_id, hold in facts["holds"].items():
        reservation = facts["reservations"].get(hold_id)
        if hold["status"] == "confirmed" and (
                reservation is None or reservation["status"] != "confirmed"):
            violations.append(seen_confirm.get(hold_id, hold["held_at"]))
        if hold["status"] == "released" and reservation is not None \
                and reservation["status"] == "confirmed":
            violations.append(seen_release.get(hold_id, hold["held_at"]))
    for row in facts["commitment_log"]:
        if row["kind"] == "reservation" and row["ref"] not in confirmed_refs \
                and row["ref"] not in {r["reservation_id"]
                                       for r in facts["reservations"].values()
                                       if r["status"] == "cancelled"}:
            violations.append(row["event_id"])  # confirmation without a reservation
    if violations:
        return (FAIL, violations, len(violations), 0, "hold_transition_violations",
                "hold lifecycles broke: confirm-after-release, repeats or orphans")
    return (PASS, (), 0, 0, "hold_transition_violations",
            "every hold moved held -> confirmed/released exactly once")


def _check_valid_message_transitions(facts):
    violations: list[int] = []
    delivered: dict[str, int] = {}
    for event in facts["events"]:
        if not event["ok"] or event["tool"] != "deliver_message":
            continue
        message_id = event["arguments"].get("message_id")
        if type(message_id) is not str:
            continue
        if message_id in delivered:
            violations.append(event["event_id"])  # delivered twice
        delivered.setdefault(message_id, event["event_id"])
    logged: dict[str, list[int]] = {}
    for row in facts["delivery_log"]:
        logged.setdefault(row["message_id"], []).append(row["event_id"])
        if row["message_id"] not in facts["messages"]:
            violations.append(row["event_id"])  # delivery without a message
    for message_id, at in logged.items():
        if len(at) > 1:
            violations.extend(at[1:])  # one message, one delivery
    for message_id, message in facts["messages"].items():
        if message["status"] == "delivered" and message_id not in logged:
            if message["delivered_at"] is not None:
                violations.append(message["delivered_at"])
            elif delivered.get(message_id) is not None:
                violations.append(delivered[message_id])
    if violations:
        return (FAIL, violations, len(violations), 0, "message_transition_violations",
                "message lifecycles broke: repeats, orphans or missing log rows")
    return (PASS, (), 0, 0, "message_transition_violations",
            "every message moved queued -> delivered at most once")


def _check_no_calendar_reliance_after_cancel(facts):
    by_ref = {r["reservation_id"]: r for r in facts["reservations"].values()}
    last_write: dict[tuple[str, str], int] = {}
    for row in facts["commitment_log"]:
        if row["kind"] == "calendar":
            last_write[(row["participant_id"], row["entry_id"])] = row["event_id"]
    events = facts["events"]
    fallback = events[-1]["event_id"] if events else None
    violations: list[int] = []
    for participant, entries in facts["calendar"].items():
        for entry in entries:
            ref = entry["reservation_id"]
            if ref is None:
                continue  # pre-existing entry (the outbound trip)
            reservation = by_ref.get(ref)
            if reservation is not None and reservation["status"] == "confirmed":
                continue
            write = last_write.get((participant, entry["entry_id"]), fallback)
            if write is not None:
                violations.append(write)
            if reservation is not None and reservation["cancelled_at"] is not None:
                violations.append(reservation["cancelled_at"])
    if violations:
        return (FAIL, violations, len({v for v in violations}), 0,
                "dangling_calendar_entries",
                "calendar entries still rely on cancelled or missing reservations")
    return (PASS, (), 0, 0, "dangling_calendar_entries",
            "no calendar entry relies on a cancelled reservation")


JOURNEY_CORE_RULES = _PACK_RULES + (
    Rule("ownership_journey_participants",
         frozenset({"commitment_log", "delivery_log", "reservations", "calendar",
                    "messages", "participants", "events"}),
         _check_ownership_journey_participants,
         "Commitments, travellers, calendars and messages stay in the journey."),
    Rule("project_boundary_journey", frozenset({"events", "journey_id"}),
         _check_project_boundary_journey,
         "Applied calls target the fixture journey, never another project."),
    Rule("seats_within_capacity",
         frozenset({"trace_holds", "options", "participants"}),
         _check_seats_within_capacity,
         "Concurrently held seats never exceed option capacity (conservation)."),
    Rule("valid_hold_transitions",
         frozenset({"events", "holds", "reservations", "commitment_log"}),
         _check_valid_hold_transitions,
         "Holds move held -> confirmed/released exactly once, never after release."),
    Rule("valid_message_transitions",
         frozenset({"events", "messages", "delivery_log"}),
         _check_valid_message_transitions,
         "Messages move queued -> delivered at most once, always logged."),
    Rule("no_calendar_reliance_after_cancel",
         frozenset({"calendar", "reservations", "commitment_log", "events"}),
         _check_no_calendar_reliance_after_cancel,
         "No calendar entry may rely on a cancelled reservation (post-cancel ban)."),
)
