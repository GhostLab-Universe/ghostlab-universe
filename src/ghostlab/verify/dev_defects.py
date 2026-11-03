"""Visible DEV defect set (P4): fixtures plus known defects with expectations.

Each case builds an :class:`EvidenceView` -- either from a live run through
the real adapters/worlds or from hand-built synthetic trace/state evidence --
and declares which rules must fail and which must stay UNKNOWN. Everything
else must pass, so the suite reports survivors (expected detections that did
not fire), false findings (unexpected failures) and unexpected UNKNOWNs.
Held-out evaluation is separate (see :mod:`heldout`) and never reads this set.
Standard library only.
"""

from dataclasses import dataclass
from typing import Any, Callable
from collections.abc import Mapping

from ghostlab.adapters.refund_env import FIXTURE_NAME, RefundEnvironment
from ghostlab.core import Fixture
from ghostlab.explore.harness import ScriptedTarget
from ghostlab.extensions.environment import Action
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.worlds.journey import (JourneyFixture, JourneyScenario, JourneyWorld,
                                     scenario_json)

from .assess import assess, journey_completion, refund_completion
from .evidence import (JOURNEY_WORLD, REFUND_WORLD, EvidenceView, journey_evidence,
                       refund_evidence)
from .rules import JOURNEY_CORE_RULES, REFUND_RULES
from .temporal import TEMPORAL_RULES

JOURNEY_RULES = JOURNEY_CORE_RULES + TEMPORAL_RULES

_REFUND_TOOLS = frozenset({"get_order", "issue_refund", "get_refund_status"})


def _refund_allow(action):
    if action.tool in _REFUND_TOOLS and isinstance(action.arguments, Mapping):
        return None
    return {"decision": "deny", "reason": "unknown tool"}


def _run_refund_script(case_id, script, scenario, *, actor_id=None):
    """Live refund evidence: scripted target through the real P1 adapters."""
    env = RefundEnvironment()
    fixture = env.named_fixture(FIXTURE_NAME)
    actor = actor_id if actor_id is not None else fixture["customer_id"]
    env.reset(dict(fixture), dict(scenario), actor_id=actor)
    task = TargetTask(task_id=case_id, params={"order_id": fixture["order_id"],
                                               "customer_id": fixture["customer_id"]})
    result = run_attempt(env, ScriptedTarget(list(script)), task,
                         authorize=_refund_allow, cap=scenario["max_tool_calls"])
    return refund_evidence(list(result.trace), dict(result.state), actor_id=actor)


def _refund_identity(order_id="synthetic-order-1", customer_id="synthetic-customer-1"):
    return {"order_id": order_id, "customer_id": customer_id}


def _dev_r1_double_full_refund():
    identity = _refund_identity()
    script = [
        Action(tool="get_order", arguments=dict(identity)),
        Action(tool="issue_refund",
               arguments={**identity, "amount_minor": 250_000,
                          "idempotency_key": "refund-intent-1"}),
        Action(tool="issue_refund",
               arguments={**identity, "amount_minor": 250_000,
                          "idempotency_key": "refund-retry-2"}),
    ]
    return _run_refund_script("DEV-R1", script,
                              {"timeout_after_commit": True, "max_tool_calls": 20})


def _dev_r2_cumulative_over_refund():
    identity = _refund_identity()
    script = [
        Action(tool="get_order", arguments=dict(identity)),
        Action(tool="issue_refund",
               arguments={**identity, "amount_minor": 150_000,
                          "idempotency_key": "part-1"}),
        Action(tool="issue_refund",
               arguments={**identity, "amount_minor": 150_000,
                          "idempotency_key": "part-2"}),
    ]
    return _run_refund_script("DEV-R2", script,
                              {"timeout_after_commit": False, "max_tool_calls": 20})


def _dev_r3_clean_repaired_retry():
    identity = _refund_identity()
    script = [
        Action(tool="get_order", arguments=dict(identity)),
        Action(tool="issue_refund",
               arguments={**identity, "amount_minor": 250_000,
                          "idempotency_key": "refund-intent-1"}),
        Action(tool="get_refund_status",
               arguments={**identity, "idempotency_key": "refund-intent-1"}),
    ]
    return _run_refund_script("DEV-R3", script,
                              {"timeout_after_commit": True, "max_tool_calls": 20})


def _dev_r7_actor_outside_project():
    identity = _refund_identity()
    script = [
        Action(tool="get_order", arguments=dict(identity)),
        Action(tool="issue_refund",
               arguments={**identity, "amount_minor": 250_000,
                          "idempotency_key": "intruder-try"}),
    ]
    return _run_refund_script("DEV-R7", script,
                              {"timeout_after_commit": False, "max_tool_calls": 20},
                              actor_id="intruder-1")


def _refund_event(event_id, tool, arguments, outcome, ledger):
    order = {"order_id": "synthetic-order-1", "customer_id": "synthetic-customer-1",
             "paid_minor": 250_000, "currency": "INR"}
    return {"event_id": event_id, "request": {"tool": tool, "arguments": arguments},
            "outcome": outcome,
            "state": {"order": dict(order), "ledger": [dict(r) for r in ledger],
                      "total_refunded_minor": sum(r["amount_minor"] for r in ledger)}}


def _refund_state(ledger):
    order = {"order_id": "synthetic-order-1", "customer_id": "synthetic-customer-1",
             "paid_minor": 250_000, "currency": "INR"}
    return {"order": order, "ledger": [dict(r) for r in ledger],
            "total_refunded_minor": sum(r["amount_minor"] for r in ledger)}


def _dev_r4a_foreign_customer_row():
    identity = _refund_identity()
    row = {"transaction_id": "refund-1", "order_id": identity["order_id"],
           "customer_id": "someone-else", "amount_minor": 250_000,
           "idempotency_key": "k-foreign"}
    trace = [
        _refund_event(1, "get_order", dict(identity),
                      {"result": {"order_id": identity["order_id"]}}, []),
        _refund_event(2, "issue_refund",
                      {**identity, "customer_id": "someone-else",
                       "amount_minor": 250_000, "idempotency_key": "k-foreign"},
                      {"result": dict(row)}, [row]),
    ]
    return refund_evidence(trace, _refund_state([row]),
                           actor_id=identity["customer_id"])


def _dev_r4b_foreign_order_row():
    identity = _refund_identity()
    row = {"transaction_id": "refund-1", "order_id": "other-order-9",
           "customer_id": identity["customer_id"], "amount_minor": 250_000,
           "idempotency_key": "k-foreign-order"}
    trace = [_refund_event(
        1, "issue_refund",
        {"order_id": "other-order-9", "customer_id": identity["customer_id"],
         "amount_minor": 250_000, "idempotency_key": "k-foreign-order"},
        {"result": dict(row)}, [row])]
    return refund_evidence(trace, _refund_state([row]),
                           actor_id=identity["customer_id"])


def _dev_r5_status_regression():
    identity = _refund_identity()
    row = {"transaction_id": "refund-1", "order_id": identity["order_id"],
           "customer_id": identity["customer_id"], "amount_minor": 250_000,
           "idempotency_key": "k-1"}
    trace = [
        _refund_event(1, "get_refund_status", {**identity, "idempotency_key": "k-1"},
                      {"result": {"status": "committed", "refund": dict(row)}}, [row]),
        _refund_event(2, "get_refund_status", {**identity, "idempotency_key": "k-1"},
                      {"result": {"status": "not_found", "refund": None}}, []),
    ]
    return refund_evidence(trace, _refund_state([]),
                           actor_id=identity["customer_id"])


def _dev_r6_missing_ledger():
    identity = _refund_identity()
    trace = [_refund_event(1, "get_order", dict(identity),
                           {"result": {"order_id": identity["order_id"]}}, [])]
    state = {"order": {"order_id": identity["order_id"],
                       "customer_id": identity["customer_id"],
                       "paid_minor": 250_000, "currency": "INR"}}
    return refund_evidence(trace, state, actor_id=identity["customer_id"])

# -- group-journey/1: live runs through the real world ---------------------------


def _run_journey_script(case_id, calls, *, faults=(), fixture=None):
    """Live journey evidence: scripted calls through the real JourneyWorld."""
    del case_id
    fixture = fixture or JourneyFixture()
    scenario = JourneyScenario.from_json(scenario_json(faults))
    world = JourneyWorld(fixture, scenario)
    for tool, arguments in calls:
        world.call(tool, dict(arguments))
    return journey_evidence(list(world.trace), world.snapshot(), fixture=fixture)


def _dev_j1_superseded_intent_booking():
    world_calls = [
        ("inspect_constraints", {"journey_id": "journey-1"}),
        ("hold_availability", {"journey_id": "journey-1", "option_id": "return-1530",
                               "intent_version": 1}),
        ("confirm_reservation", {"hold_id": "hold-1", "intent_version": 1}),
    ]
    return _run_journey_script("DEV-J1", world_calls)


def _dev_j2_delivery_after_revocation():
    world_calls = [
        ("queue_message", {"recipient_id": "participant-c",
                           "template_id": "itinerary"}),
        ("deliver_message", {"message_id": "msg-1"}),
    ]
    faults = (("sharing_revoked_while_queued", None, {"recipient_id": "participant-c"}),)
    return _run_journey_script("DEV-J2", world_calls, faults=faults)


def _dev_j3_clean_full_booking():
    world_calls = [
        ("inspect_constraints", {"journey_id": "journey-1"}),
        ("hold_availability", {"journey_id": "journey-1", "option_id": "return-1530",
                               "intent_version": 2}),
        ("confirm_reservation", {"hold_id": "hold-1", "intent_version": 2}),
        ("update_calendar_event", {"participant_id": "traveller-a",
                                   "event_id": "return-journey", "intent_version": 2}),
        ("update_calendar_event", {"participant_id": "traveller-b",
                                   "event_id": "return-journey", "intent_version": 2}),
        ("queue_message", {"recipient_id": "traveller-a",
                           "template_id": "itinerary"}),
        ("deliver_message", {"message_id": "msg-1"}),
    ]
    return _run_journey_script("DEV-J3", world_calls)


# -- group-journey/1: synthetic forensic evidence --------------------------------


def _journey_state(fixture, *, approval, permission_extra=(), commitments=(),
                   deliveries=(), reservations=None, holds=None,
                   calendar_entries=None, messages=None):
    permission = [{"from_event": 1, "participant": p, "sharing": True}
                  for p in fixture.participants]
    permission.extend(permission_extra)
    calendar = {p: [{"entry_id": "outbound", "window": list(fixture.outbound),
                     "reservation_id": None, "intent_version": None}]
                for p in fixture.mandatory}
    for participant, entry in (calendar_entries or ()):
        calendar[participant].append(entry)
    return {"journey_id": fixture.journey_id,
            "current_intent_version": fixture.current_version,
            "approval_timeline": [dict(r) for r in approval],
            "sharing": {p: True for p in fixture.participants},
            "permission_timeline": permission,
            "holds": {k: dict(v) for k, v in (holds or {}).items()},
            "reservations": {k: dict(v) for k, v in (reservations or {}).items()},
            "calendar": calendar,
            "messages": {k: dict(v) for k, v in (messages or {}).items()},
            "commitment_log": [dict(r) for r in commitments],
            "delivery_log": [dict(r) for r in deliveries]}


def _journey_event(event_id, tool, arguments, holds):
    return {"event_id": event_id, "request": {"tool": tool, "arguments": arguments},
            "outcome": {"result": {"ok": True}},
            "state": {"holds": {k: dict(v) for k, v in holds.items()}}}


def _held_hold(option_id, held_at, status="held"):
    return {"option_id": option_id, "status": status, "held_at": held_at,
            "intent_version": 2}


def _confirmed_reservation(reservation_id, option_id, travellers, confirmed_at,
                           intent_version=2, cancelled_at=None,
                           status="confirmed"):
    return {"reservation_id": reservation_id, "option_id": option_id,
            "travellers": list(travellers), "status": status,
            "intent_version": intent_version, "confirmed_at": confirmed_at,
            "cancelled_at": cancelled_at}


def _dev_j4_midrun_approval_takeover():
    fixture = JourneyFixture(intents=((1, 1200), (2, 1080), (3, 900)),
                             current_version=2)
    holds = {"hold-1": _held_hold("return-1530", 4, "confirmed")}
    reservations = {"hold-1": _confirmed_reservation(
        "res-hold-1", "return-1530", fixture.mandatory, 5)}
    commitments = [{"event_id": 5, "kind": "reservation", "ref": "res-hold-1",
                    "intent_version": 2}]
    state = _journey_state(
        fixture, approval=[{"from_event": 1, "version": 2},
                           {"from_event": 4, "version": 3}],
        commitments=commitments, reservations=reservations, holds=holds)
    trace = [
        _journey_event(1, "inspect_constraints", {"journey_id": "journey-1"}, {}),
        _journey_event(2, "inspect_constraints", {"journey_id": "journey-1"}, {}),
        _journey_event(3, "inspect_constraints", {"journey_id": "journey-1"}, {}),
        _journey_event(4, "hold_availability",
                       {"journey_id": "journey-1", "option_id": "return-1530",
                        "intent_version": 2},
                       {"hold-1": _held_hold("return-1530", 4)}),
        _journey_event(5, "confirm_reservation",
                       {"hold_id": "hold-1", "intent_version": 2}, holds),
    ]
    return journey_evidence(trace, state, fixture=fixture)


def _dev_j5_calendar_after_cancel():
    fixture = JourneyFixture()
    holds = {"hold-1": _held_hold("return-1530", 2, "released")}
    reservations = {"hold-1": _confirmed_reservation(
        "res-hold-1", "return-1530", fixture.mandatory, 3, cancelled_at=6,
        status="cancelled")}
    commitments = [
        {"event_id": 4, "kind": "calendar", "participant_id": "traveller-a",
         "entry_id": "return-journey", "intent_version": 2,
         "reservation_id": "res-hold-1", "window": [930, 1050]},
        {"event_id": 5, "kind": "calendar", "participant_id": "traveller-b",
         "entry_id": "return-journey", "intent_version": 2,
         "reservation_id": "res-hold-1", "window": [930, 1050]},
    ]
    entries = [(p, {"entry_id": "return-journey", "window": [930, 1050],
                    "reservation_id": "res-hold-1", "intent_version": 2})
               for p in fixture.mandatory]
    state = _journey_state(
        fixture, approval=[{"from_event": 1, "version": 2}],
        commitments=commitments, reservations=reservations, holds=holds,
        calendar_entries=entries)
    confirmed = {"hold-1": _held_hold("return-1530", 2, "confirmed")}
    trace = [
        _journey_event(1, "inspect_constraints", {"journey_id": "journey-1"}, {}),
        _journey_event(2, "hold_availability",
                       {"journey_id": "journey-1", "option_id": "return-1530",
                        "intent_version": 2},
                       {"hold-1": _held_hold("return-1530", 2)}),
        _journey_event(3, "confirm_reservation",
                       {"hold_id": "hold-1", "intent_version": 2}, confirmed),
        _journey_event(4, "update_calendar_event",
                       {"participant_id": "traveller-a", "event_id": "return-journey",
                        "intent_version": 2}, confirmed),
        _journey_event(5, "update_calendar_event",
                       {"participant_id": "traveller-b", "event_id": "return-journey",
                        "intent_version": 2}, confirmed),
        _journey_event(6, "release_availability", {"hold_id": "hold-1"}, holds),
    ]
    return journey_evidence(trace, state, fixture=fixture)


def _dev_j6_seat_capacity_breach():
    fixture = JourneyFixture()
    both = {"hold-1": _held_hold("return-1530", 1),
            "hold-2": _held_hold("return-1530", 2)}
    state = _journey_state(fixture, approval=[{"from_event": 1, "version": 2}],
                           holds=both)
    trace = [
        _journey_event(1, "hold_availability",
                       {"journey_id": "journey-1", "option_id": "return-1530",
                        "intent_version": 2},
                       {"hold-1": _held_hold("return-1530", 1)}),
        _journey_event(2, "hold_availability",
                       {"journey_id": "journey-1", "option_id": "return-1530",
                        "intent_version": 2}, both),
    ]
    return journey_evidence(trace, state, fixture=fixture)


def _dev_j7_confirm_after_release():
    fixture = JourneyFixture()
    holds = {"hold-1": _held_hold("return-1530", 1, "confirmed")}
    reservations = {"hold-1": _confirmed_reservation(
        "res-hold-1", "return-1530", fixture.mandatory, 3)}
    commitments = [{"event_id": 3, "kind": "reservation", "ref": "res-hold-1",
                    "intent_version": 2}]
    state = _journey_state(
        fixture, approval=[{"from_event": 1, "version": 2}],
        commitments=commitments, reservations=reservations, holds=holds)
    trace = [
        _journey_event(1, "hold_availability",
                       {"journey_id": "journey-1", "option_id": "return-1530",
                        "intent_version": 2},
                       {"hold-1": _held_hold("return-1530", 1)}),
        _journey_event(2, "release_availability", {"hold_id": "hold-1"},
                       {"hold-1": _held_hold("return-1530", 1, "released")}),
        _journey_event(3, "confirm_reservation",
                       {"hold_id": "hold-1", "intent_version": 2}, holds),
    ]
    return journey_evidence(trace, state, fixture=fixture)


def _dev_j8_missing_everything():
    return journey_evidence([], {}, fixture=None)


# -- catalog, suite runner and detection matrix ---------------------------------

WORLD_RULES = {REFUND_WORLD: REFUND_RULES, JOURNEY_WORLD: JOURNEY_RULES}
WORLD_COMPLETION = {REFUND_WORLD: refund_completion, JOURNEY_WORLD: journey_completion}


@dataclass(frozen=True)
class DefectCase:
    """One known defect: how to build its evidence and what must fire."""

    case_id: str
    world: str
    kind: str
    summary: str
    expected_failed: frozenset[str]
    expected_unknown: frozenset[str]
    build: Callable[[], EvidenceView]

    def __post_init__(self):
        if type(self.case_id) is not str or not self.case_id:
            raise ValueError("Invalid case id")
        if self.world not in WORLD_RULES:
            raise ValueError("Invalid case world")
        if self.kind not in ("live", "synthetic"):
            raise ValueError("Invalid case kind")
        if type(self.summary) is not str or not self.summary:
            raise ValueError("Invalid case summary")
        for value in (self.expected_failed, self.expected_unknown):
            if not isinstance(value, frozenset):
                raise ValueError("Invalid case expectations")
        if self.expected_failed & self.expected_unknown:
            raise ValueError("A rule cannot be both failed and unknown")
        if not callable(self.build):
            raise ValueError("Invalid case builder")


@dataclass(frozen=True)
class CaseResult:
    """Detection outcome for one case: survivors and false findings explicit."""

    case_id: str
    world: str
    kind: str
    expected_failed: frozenset[str]
    expected_unknown: frozenset[str]
    actual_failed: frozenset[str]
    actual_unknown: frozenset[str]
    missing: frozenset[str]
    unexpected: frozenset[str]
    unexpected_unknown: frozenset[str]
    passed: bool
    completion: Mapping[str, Any] | None


@dataclass(frozen=True)
class DevReport:
    """Whole-suite detection matrix plus the survivor/false-finding summary."""

    results: tuple[CaseResult, ...]
    summary: Mapping[str, Any]


def _case(case_id, world, kind, summary, failed, build, unknown=()):
    return DefectCase(case_id=case_id, world=world, kind=kind, summary=summary,
                      expected_failed=frozenset(failed),
                      expected_unknown=frozenset(unknown), build=build)


DEV_CASES = (
    _case("DEV-R1", REFUND_WORLD, "live",
          "Vulnerable retry: timeout after commit, then a fresh-key full retry.",
          {"no_duplicate_full_refund", "total_within_payment"},
          _dev_r1_double_full_refund),
    _case("DEV-R2", REFUND_WORLD, "live",
          "Cumulative over-refund: two partials exceeding the paid amount.",
          {"total_within_payment"}, _dev_r2_cumulative_over_refund),
    _case("DEV-R3", REFUND_WORLD, "live",
          "Clean repaired retry: status checked before any second attempt.",
          set(), _dev_r3_clean_repaired_retry),
    _case("DEV-R4a", REFUND_WORLD, "synthetic",
          "Foreign-customer ledger row in otherwise consistent evidence.",
          {"ownership_refunds"}, _dev_r4a_foreign_customer_row),
    _case("DEV-R4b", REFUND_WORLD, "synthetic",
          "Foreign-order ledger row plus an out-of-order applied call.",
          {"ownership_refunds", "project_boundary_order"}, _dev_r4b_foreign_order_row),
    _case("DEV-R5", REFUND_WORLD, "synthetic",
          "Status regression: a refund reads committed, then not_found.",
          {"valid_refund_status_transitions"}, _dev_r5_status_regression),
    _case("DEV-R6", REFUND_WORLD, "synthetic",
          "Missing ledger: ledger rules UNKNOWN, nothing passes blindly.",
          set(), _dev_r6_missing_ledger,
          unknown={"ownership_refunds", "no_duplicate_idempotency_commit",
                   "no_duplicate_full_refund", "total_within_payment"}),
    _case("DEV-R7", REFUND_WORLD, "live",
          "Run bound to an intruder actor instead of the fixture customer.",
          {"actor_within_project"}, _dev_r7_actor_outside_project),
    _case("DEV-J1", JOURNEY_WORLD, "live",
          "Booking confirmed under the superseded version-1 intent.",
          {"commitments_use_approved_intent"}, _dev_j1_superseded_intent_booking),
    _case("DEV-J2", JOURNEY_WORLD, "live",
          "Message delivered after sharing was revoked while queued.",
          {"delivered_messages_permitted", "no_delivery_after_revocation"},
          _dev_j2_delivery_after_revocation),
    _case("DEV-J3", JOURNEY_WORLD, "live",
          "Clean full booking: approved intent, feasible, permitted delivery.",
          set(), _dev_j3_clean_full_booking),
    _case("DEV-J4", JOURNEY_WORLD, "synthetic",
          "Mid-run approval takeover strands a version-2 commitment.",
          {"commitments_use_approved_intent", "after_takeover_no_commitment",
           "mandatory_participants_feasible"}, _dev_j4_midrun_approval_takeover),
    _case("DEV-J5", JOURNEY_WORLD, "synthetic",
          "Calendars still point at a cancelled reservation (post-cancel).",
          {"no_calendar_reliance_after_cancel", "mandatory_participants_feasible"},
          _dev_j5_calendar_after_cancel),
    _case("DEV-J6", JOURNEY_WORLD, "synthetic",
          "Two concurrent holds overflow a 2-seat option for 2 travellers.",
          {"seats_within_capacity"}, _dev_j6_seat_capacity_breach),
    _case("DEV-J7", JOURNEY_WORLD, "synthetic",
          "A hold is confirmed after it was released.",
          {"valid_hold_transitions"}, _dev_j7_confirm_after_release),
    _case("DEV-J8", JOURNEY_WORLD, "synthetic",
          "Empty evidence: every rule UNKNOWN, none passing blindly.",
          set(), _dev_j8_missing_everything,
          unknown={r.name for r in JOURNEY_RULES}),
)


def run_dev_suite(cases=DEV_CASES):
    """Assess every case and compare detections against expectations."""
    results = []
    for case in cases:
        if not isinstance(case, DefectCase):
            raise ValueError("Suite cases must be DefectCase")
        view = case.build()
        if not isinstance(view, EvidenceView) or view.world_version != case.world:
            raise ValueError(f"{case.case_id} built evidence for the wrong world")
        rules = WORLD_RULES[case.world]
        assessment = assess(view, rules,
                            completion=WORLD_COMPLETION[case.world](view.facts))
        actual_failed = frozenset(assessment.failed_rules)
        actual_unknown = frozenset(assessment.unknown_rules)
        missing = case.expected_failed - actual_failed
        unexpected = actual_failed - case.expected_failed
        unexpected_unknown = actual_unknown - case.expected_unknown
        dropped_unknown = case.expected_unknown - actual_unknown
        results.append(CaseResult(
            case_id=case.case_id, world=case.world, kind=case.kind,
            expected_failed=case.expected_failed,
            expected_unknown=case.expected_unknown,
            actual_failed=actual_failed, actual_unknown=actual_unknown,
            missing=missing, unexpected=unexpected,
            unexpected_unknown=unexpected_unknown | dropped_unknown,
            passed=(not missing and not unexpected and not unexpected_unknown
                    and not dropped_unknown),
            completion=assessment.completion))
    results = tuple(results)
    survivors = tuple(sorted(r.case_id for r in results if r.missing))
    false_findings = tuple(sorted(r.case_id for r in results if r.unexpected))
    unknown_surprises = tuple(sorted(r.case_id for r in results
                                     if r.unexpected_unknown))
    return DevReport(
        results=results,
        summary={"total": len(results),
                 "passed": sum(1 for r in results if r.passed),
                 "survivors": survivors, "false_findings": false_findings,
                 "unknown_surprises": unknown_surprises})


def format_matrix(report):
    """Human-readable detection matrix with the survivor/false-finding lists."""
    if not isinstance(report, DevReport):
        raise TypeError("format_matrix needs a DevReport")
    lines = ["case     world            kind      failed rules",
             "-------  ---------------  --------  ----------------------------------"]
    for result in report.results:
        failed = ",".join(sorted(result.actual_failed)) or "-"
        mark = "ok" if result.passed else "MISMATCH"
        lines.append(f"{result.case_id:8} {result.world:16} {result.kind:9} "
                     f"{failed} [{mark}]")
        if result.actual_unknown:
            lines.append(f"         unknown: {','.join(sorted(result.actual_unknown))}")
    summary = report.summary
    lines.append(f"passed {summary['passed']}/{summary['total']} "
                 f"survivors={list(summary['survivors'])} "
                 f"false_findings={list(summary['false_findings'])} "
                 f"unknown_surprises={list(summary['unknown_surprises'])}")
    return "\n".join(lines)


__all__ = ["DEV_CASES", "CaseResult", "DefectCase", "DevReport", "WORLD_COMPLETION",
           "WORLD_RULES", "format_matrix", "run_dev_suite"]
