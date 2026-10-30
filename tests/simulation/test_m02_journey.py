"""M02 group-journey/1: world, guard, verifier, adapters, diff and v2 capsule replay.

Stdlib and pytest only (no Pydantic); contract conformance lives in
test_m02_journey_contracts.py. No __init__.py here, so the bare-interpreter
core suite is unaffected.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ghostlab import capsule as refund_capsule
from ghostlab.cli import main
from ghostlab.execution.journey_guard import (GuardedJourneyWorld, PermissionWithdrawnDenied,
                                              StaleIntentDenied, make_world)
from ghostlab.worlds import journey_capsule
from ghostlab.worlds.journey import (FAULTS, READ_TOOLS, TOOL_SCHEMAS, BudgetExceeded,
                                     HostDenied, JourneyFixture, JourneyOption, JourneyScenario,
                                     JourneyWorld, ToolError, scenario_json)
from ghostlab.worlds.journey_run import (ADAPTERS, experiment, fault_scenarios, findings,
                                         race_scenario, reality_diff, run)
from ghostlab.worlds.journey_verify import (INVARIANTS, approved_version_at, task_outcome,
                                            verify, verify_details)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "contracts"
FIXTURE = JourneyFixture()
GUARDED = JourneyFixture(guarded=True)
CLEAN = JourneyScenario.from_json(scenario_json())
NAIVE, CHECKED = "journey-naive", "journey-version-checked"


@pytest.fixture(scope="module")
def result():
    return experiment(FIXTURE)


def failed(recorded, fixture=FIXTURE):
    return {name: detail["evidence_event_ids"]
            for name, detail in verify_details(fixture, recorded["final_state"]).items()
            if not detail["passed"]}


def denied(recorded):
    return [(e["event_id"], e["outcome"]["error"]) for e in recorded["trace"]
            if "error" in e["outcome"]]


def event(recorded, event_id):
    return recorded["trace"][event_id - 1]


def scenario(*faults, max_tool_calls=40):
    return JourneyScenario.from_json(scenario_json(faults, max_tool_calls))


# --- the pack manifest describes this world -------------------------------------------

def test_pack_manifest_matches_the_world():
    manifest = json.loads((EXAMPLES / "core" / "world_pack.group_journey.json")
                          .read_text(encoding="utf-8"))
    assert manifest["world_version"] == "group-journey/1"
    for tool in manifest["tools"]:
        schema = tool["parameters"]
        assert set(schema["required"]) == set(schema["properties"]) == set(TOOL_SCHEMAS[tool["name"]])
        for name, spec in schema["properties"].items():
            kind, (low, high) = TOOL_SCHEMAS[tool["name"]][name]
            assert spec["type"] == kind
            bounds = ((spec["minLength"], spec["maxLength"]) if kind == "string"
                      else (spec["minimum"], spec["maximum"]))
            assert bounds == (low, high)
        assert (tool["effect"] == "read") == (tool["name"] in READ_TOOLS)
    assert {t["name"] for t in manifest["tools"]} == set(TOOL_SCHEMAS)
    types = {"integer": int, "string": str, "boolean": bool}
    assert {f["fault_id"]: {p["name"]: types[p["type"]] for p in f["parameters"]}
            for f in manifest["faults"]} == FAULTS
    assert [i["name"] for i in manifest["invariants"]] == list(INVARIANTS)
    assert set(verify(FIXTURE, JourneyWorld(FIXTURE, CLEAN).snapshot())) == set(INVARIANTS)
    assert {a["adapter_id"]: a["kind"] for a in manifest["adapters"]} == {
        NAIVE: "vulnerable", CHECKED: "repaired"}
    assert set(ADAPTERS) == {NAIVE, CHECKED}


# --- the expected matrix --------------------------------------------------------------

def test_baseline_passes_for_both_adapters_and_completes_the_task(result):
    for recorded in result["baseline"].values():
        assert recorded["status"] == "completed"
        assert failed(recorded) == {} and denied(recorded) == []
        assert task_outcome(FIXTURE, recorded["final_state"]) == {
            "reservation_confirmed": True, "calendars_updated": True,
            "mandatory_informed": True, "final_itinerary_feasible": True}


@pytest.mark.parametrize("fault_id, violated, guard_error, guarded_task_done", [
    ("stale_preference_memory",
     {"commitments_use_approved_intent", "mandatory_participants_feasible"},
     "StaleIntentDenied", False),
    ("delayed_itinerary_update",
     {"commitments_use_approved_intent", "mandatory_participants_feasible"},
     "StaleIntentDenied", True),  # the first booking was under v2; only the rebook is denied
    ("sharing_revoked_while_queued", {"delivered_messages_permitted"},
     "PermissionWithdrawnDenied", True),
])
def test_fault_matrix(result, fault_id, violated, guard_error, guarded_task_done):
    case = result["faults"][fault_id]
    assert set(failed(case["faulted"])) == violated
    assert case["diff"]["violated_invariants"] == [n for n in INVARIANTS if n in violated]
    assert failed(case["repaired"]) == {} and denied(case["repaired"]) == []
    assert task_outcome(FIXTURE, case["repaired"]["final_state"])["calendars_updated"]
    guarded = case["guarded"]
    assert failed(guarded, GUARDED) == {}
    assert [error for _, error in denied(guarded)] == [guard_error]
    assert task_outcome(GUARDED, guarded["final_state"])["calendars_updated"] is guarded_task_done


def test_stale_memory_evidence_points_at_the_v1_booking(result):
    faulted = result["faults"]["stale_preference_memory"]["faulted"]
    evidence = failed(faulted)
    tools = [event(faulted, e)["request"]["tool"]
             for e in evidence["commitments_use_approved_intent"]]
    assert tools == ["confirm_reservation", "update_calendar_event", "update_calendar_event"]
    confirm = event(faulted, evidence["mandatory_participants_feasible"][0])
    assert confirm["outcome"]["result"]["option_id"] == "return-1700"  # arrives 19:00 > 18:00
    assert confirm["request"]["arguments"]["intent_version"] == 1
    # first divergence is the fault onset: memory resurfaced at the first inspect
    divergence = result["faults"]["stale_preference_memory"]["diff"]["first_divergence"]
    assert divergence["event_id"] == 1
    assert "remembered_intent" in divergence["faulted"]["outcome"]["result"]


def test_delayed_update_rebooks_under_v1_and_double_books(result):
    faulted = result["faults"]["delayed_itinerary_update"]["faulted"]
    state = faulted["final_state"]
    confirmed = sorted((r["confirmed_at"], r["intent_version"], r["option_id"])
                       for r in state["reservations"].values() if r["status"] == "confirmed")
    assert confirmed == [(3, 2, "return-1530"), (8, 1, "return-1700")]
    # the calendar follows the rebooking, so the evidence is coherent
    assert {e["reservation_id"] for p in FIXTURE.mandatory for e in state["calendar"][p]
            if e["entry_id"] == "return-journey"} == {"res-hold-2"}
    assert failed(faulted)["mandatory_participants_feasible"] == [3, 8]
    divergence = result["faults"]["delayed_itinerary_update"]["diff"]["first_divergence"]
    assert "incoming_update" in divergence["faulted"]["outcome"]["result"]
    assert approved_version_at(state, 8) == 2


def test_revocation_evidence_is_the_delivery_not_the_onset(result):
    case = result["faults"]["sharing_revoked_while_queued"]
    [delivery] = failed(case["faulted"])["delivered_messages_permitted"]
    delivered = event(case["faulted"], delivery)
    assert delivered["request"]["tool"] == "deliver_message"
    message = delivered["request"]["arguments"]["message_id"]
    assert case["faulted"]["final_state"]["messages"][message]["recipient_id"] == "participant-c"
    onset = case["diff"]["first_divergence"]["event_id"]
    assert onset < delivery  # the diff shows the onset; findings cite the violation
    assert case["diff"]["faulted_metrics"]["messages_delivered_without_permission"] == 1
    assert case["diff"]["baseline_metrics"]["messages_delivered_without_permission"] == 0
    [finding] = [f for f in findings(FIXTURE, case["faulted"], "journey-run-1",
                                     "2026-10-30T00:00:00Z") if f["verdict"] == "failed"]
    assert finding["evidence_event_ids"] == [delivery]
    assert finding["unit"] == "unpermitted_deliveries" and finding["observed_value"] == 1


def test_version_checked_leaves_a_revoked_message_queued(result):
    repaired = result["faults"]["sharing_revoked_while_queued"]["repaired"]
    messages = repaired["final_state"]["messages"].values()
    assert [(m["recipient_id"], m["status"]) for m in messages] == [
        ("traveller-a", "delivered"), ("traveller-b", "delivered"),
        ("participant-c", "queued")]


# --- the check-then-deliver race: only the guard closes it ------------------------------

def test_revocation_race_gets_through_the_lab_world_and_only_the_guard_stops_it(result):
    race = result["race"]
    revoked_after = race["scenario"]["faults"][0]["after_event"]
    lab = race["lab"]
    [delivery] = failed(lab)["delivered_messages_permitted"]
    # the trigger is derived, and the revocation lands exactly on the delivery event
    assert [row["from_event"] for row in lab["final_state"]["permission_timeline"]
            if not row["sharing"]] == [delivery] == [revoked_after + 1]
    check = event(lab, delivery - 1)
    assert check["request"]["tool"] == "inspect_constraints"
    assert {p["participant_id"]: p["sharing"]
            for p in check["outcome"]["result"]["participants"]}["participant-c"] is True

    guarded = race["guarded"]
    assert failed(guarded, GUARDED) == {}
    assert denied(guarded) == [(delivery, "PermissionWithdrawnDenied")]
    assert all(row["event_id"] != delivery
               for row in guarded["final_state"]["delivery_log"])
    assert race["diff"]["violated_invariants"] == ["delivered_messages_permitted"]


def test_delivery_before_revocation_stays_permitted():
    world = JourneyWorld(FIXTURE, scenario(("sharing_revoked_while_queued", 3,
                                            {"recipient_id": "participant-c"})))
    message = world.call("queue_message", {"recipient_id": "participant-c",
                                           "template_id": "itinerary-v2"})
    world.call("deliver_message", {"message_id": message["message_id"]})
    world.call("inspect_constraints", {"journey_id": "journey-1"})
    world.call("inspect_constraints", {"journey_id": "journey-1"})  # revoked from event 4
    assert world.state["sharing"]["participant-c"] is False
    assert verify(FIXTURE, world.snapshot())["delivered_messages_permitted"] is True


# --- the guard changes nothing it denies -----------------------------------------------

UNCHANGED = ("holds", "reservations", "calendar", "messages", "commitment_log",
             "delivery_log", "approval_timeline", "current_intent_version")


@pytest.mark.parametrize("calls, error", [
    ([("hold_availability", {"journey_id": "journey-1", "option_id": "return-1530",
                             "intent_version": 1})], StaleIntentDenied),
    ([("hold_availability", {"journey_id": "journey-1", "option_id": "return-1530",
                             "intent_version": 2}),
      ("confirm_reservation", {"hold_id": "hold-1", "intent_version": 1})], StaleIntentDenied),
    ([("hold_availability", {"journey_id": "journey-1", "option_id": "return-1530",
                             "intent_version": 2}),
      ("confirm_reservation", {"hold_id": "hold-1", "intent_version": 2}),
      ("update_calendar_event", {"participant_id": "traveller-b", "event_id": "return",
                                 "intent_version": 1})], StaleIntentDenied),
    ([("queue_message", {"recipient_id": "participant-c", "template_id": "t"}),
      ("deliver_message", {"message_id": "msg-1"})], PermissionWithdrawnDenied),
])
def test_guard_denials_record_an_error_event_and_change_nothing(calls, error):
    world = make_world(GUARDED, scenario(("sharing_revoked_while_queued", None,
                                          {"recipient_id": "participant-c"})))
    assert isinstance(world, GuardedJourneyWorld)
    *setup, (tool, arguments) = calls
    for name, args in setup:
        world.call(name, args)
    before = world.snapshot()
    with pytest.raises(error):
        world.call(tool, arguments)
    after = world.trace[-1]
    assert after["outcome"]["error"] == error.__name__
    # pending revocations may take effect at a denied event; nothing else may change
    assert {k: after["state"][k] for k in UNCHANGED} == {k: before[k] for k in UNCHANGED}


def test_the_lab_world_permits_the_same_mistakes():
    world = make_world(FIXTURE, CLEAN)
    assert type(world) is JourneyWorld
    hold = world.call("hold_availability", {"journey_id": "journey-1",
                                            "option_id": "return-1700", "intent_version": 1})
    world.call("confirm_reservation", {"hold_id": hold["hold_id"], "intent_version": 1})
    assert verify(FIXTURE, world.snapshot()) == {
        "commitments_use_approved_intent": False, "mandatory_participants_feasible": False,
        "delivered_messages_permitted": True}


# --- host checks are always on ---------------------------------------------------------

@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("tool, arguments", [
    ("delete_journey", {"journey_id": "journey-1"}),
    ("inspect_constraints", {"journey_id": "journey-1", "actor_id": "traveller-b"}),
    ("inspect_constraints", {}),
    ("inspect_constraints", {"journey_id": "journey-2"}),
    ("inspect_constraints", {"journey_id": ""}),
    ("inspect_constraints", {"journey_id": "x" * 101}),
    ("inspect_constraints", {"journey_id": 1}),
    ("hold_availability", {"journey_id": "journey-1", "option_id": "return-1530",
                           "intent_version": True}),
    ("hold_availability", {"journey_id": "journey-1", "option_id": "return-1530",
                           "intent_version": 0}),
    ("hold_availability", {"journey_id": "journey-1", "option_id": "return-9999",
                           "intent_version": 2}),
    ("hold_availability", {"journey_id": "journey-1", "option_id": "return-1530",
                           "intent_version": 7}),
    ("confirm_reservation", {"hold_id": "hold-9", "intent_version": 2}),
    ("release_availability", {"hold_id": "hold-9"}),
    ("update_calendar_event", {"participant_id": "participant-c", "event_id": "e",
                               "intent_version": 2}),
    ("queue_message", {"recipient_id": "stranger", "template_id": "t"}),
    ("deliver_message", {"message_id": "msg-9"}),
    ("inspect_constraints", ["journey-1"]),
])
def test_malformed_unknown_and_unreferenced_calls_are_denied(guarded, tool, arguments):
    world = make_world(replace(FIXTURE, guarded=guarded), CLEAN)
    before = world.snapshot()
    with pytest.raises(HostDenied):
        world.call(tool, arguments)
    assert world.trace[-1]["outcome"]["error"] == "HostDenied"
    assert world.snapshot() == before


@pytest.mark.parametrize("tool, arguments", [
    ("hold_availability", {"journey_id": "journey-1", "option_id": "return-9999",
                           "intent_version": 1}),
    ("confirm_reservation", {"hold_id": "hold-9", "intent_version": 1}),
    ("update_calendar_event", {"participant_id": "participant-c", "event_id": "e",
                               "intent_version": 1}),
])
def test_host_reference_checks_precede_the_guard(tool, arguments):
    world = make_world(GUARDED, CLEAN)
    with pytest.raises(HostDenied):  # a stale version on a bad reference is still HostDenied
        world.call(tool, arguments)


@pytest.mark.parametrize("actor, read_allowed", [
    ("traveller-b", True), ("participant-c", True), ("stranger", False)])
def test_only_the_bound_organizer_may_change_the_journey(actor, read_allowed):
    world = JourneyWorld(FIXTURE, CLEAN, actor_id=actor)
    if read_allowed:
        world.call("inspect_constraints", {"journey_id": "journey-1"})
    else:
        with pytest.raises(HostDenied):
            world.call("inspect_constraints", {"journey_id": "journey-1"})
    with pytest.raises(HostDenied):
        world.call("queue_message", {"recipient_id": "participant-c", "template_id": "t"})
    assert world.state["messages"] == {}


@pytest.mark.parametrize("adapter", list(ADAPTERS))
def test_a_spoofed_principal_cannot_act_through_an_adapter(adapter):
    recorded = run(FIXTURE, CLEAN, adapter, actor_id="participant-c")
    assert recorded["actor_id"] == "participant-c"
    assert recorded["final_state"]["commitment_log"] == []
    assert recorded["final_state"]["delivery_log"] == []
    assert {error for _, error in denied(recorded)} == {"HostDenied"}


@pytest.mark.parametrize("actor_id", ["", "x" * 101, 7])
def test_invalid_principals_are_rejected(actor_id):
    with pytest.raises(ValueError):
        JourneyWorld(FIXTURE, CLEAN, actor_id=actor_id)


# --- budgets, isolation and determinism -------------------------------------------------

@pytest.mark.parametrize("adapter", list(ADAPTERS))
@pytest.mark.parametrize("budget", [1, 5, 11])
def test_the_budget_is_checked_before_each_call(adapter, budget):
    recorded = run(FIXTURE, scenario(max_tool_calls=budget), adapter)
    assert recorded["status"] == "budget_exhausted"
    assert len(recorded["trace"]) == budget
    world = JourneyWorld(FIXTURE, scenario(max_tool_calls=1))
    world.call("inspect_constraints", {"journey_id": "journey-1"})
    with pytest.raises(BudgetExceeded):
        world.call("inspect_constraints", {"journey_id": "journey-1"})
    assert len(world.trace) == 1


def test_runs_are_deterministic_and_branches_share_no_state(result):
    assert experiment(FIXTURE) == result
    baseline = result["baseline"][NAIVE]
    again = run(FIXTURE, CLEAN, NAIVE)
    again["final_state"]["holds"]["hold-1"]["status"] = "tampered"
    again["trace"][0]["state"]["sharing"]["participant-c"] = False
    assert baseline["final_state"]["holds"]["hold-1"]["status"] == "confirmed"
    assert run(FIXTURE, CLEAN, NAIVE) == baseline


def test_world_results_and_snapshots_are_copies():
    world = JourneyWorld(FIXTURE, CLEAN)
    seen = world.call("inspect_constraints", {"journey_id": "journey-1"})
    seen["participants"][2]["sharing"] = False
    world.trace[0]["state"]["sharing"]["participant-c"] = False
    assert world.state["sharing"]["participant-c"] is True
    with pytest.raises(AttributeError):
        FIXTURE.guarded = True  # frozen


@pytest.mark.parametrize("change", [
    {"organizer": "participant-c"}, {"current_version": 3}, {"intents": ((1, 1200), (1, 1080))},
    {"intents": (("1", 1200), (2, 1080))}, {"guarded": 1}, {"outbound": (600, 480)},
    {"viewers": ("traveller-b",)}, {"journey_id": ""}, {"outbound": (480, True)},
])
def test_invalid_fixtures_are_rejected(change):
    with pytest.raises(ValueError):
        replace(FIXTURE, **change)


def test_fixture_json_round_trips():
    assert JourneyFixture.from_json(GUARDED.to_json()) == GUARDED
    assert json.loads(json.dumps(FIXTURE.to_json())) == FIXTURE.to_json()


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(world_version="refund-world/1"),
    lambda s: s.update(schema_version=True),
    lambda s: s.update(max_tool_calls=0),
    lambda s: s.update(extra=1),
    lambda s: s["faults"].append(dict(s["faults"][0])),
    lambda s: s["faults"][0].update(fault_id="unknown_fault"),
    lambda s: s["faults"][0].update(after_event=0),
    lambda s: s["faults"][0].pop("parameters"),
    lambda s: s["faults"][0]["parameters"][0].update(value=3),  # recipient_id is a string
    lambda s: s["faults"][0]["parameters"].append({"name": "recipient_id", "value": "x"}),
    lambda s: s["faults"][0]["parameters"].__setitem__(0, ["recipient_id", "x"]),
])
def test_invalid_scenarios_are_rejected(mutate):
    data = scenario_json([("sharing_revoked_while_queued", 4, {"recipient_id": "participant-c"})])
    mutate(data)
    with pytest.raises(ValueError):
        JourneyScenario.from_json(data)


def test_delayed_update_is_timed_by_delay_events_alone():
    with pytest.raises(ValueError):
        scenario(("delayed_itinerary_update", 3, {"delay_events": 2}))


def test_the_published_example_scenario_runs():
    # Owned by M00; assert only what any revocation example must show.
    data = json.loads((EXAMPLES / "world_scenario.group_journey_revocation.json")
                      .read_text(encoding="utf-8"))
    published = JourneyScenario.from_json(data)
    assert "delivered_messages_permitted" in failed(run(FIXTURE, published, NAIVE))
    assert failed(run(FIXTURE, published, CHECKED)) == {}
    assert failed(run(GUARDED, published, NAIVE), GUARDED) == {}


# --- the verifier reads evidence, not live fields ---------------------------------------

def test_verifier_ignores_the_live_fields_the_guard_reads(result):
    case = result["faults"]["sharing_revoked_while_queued"]
    state = json.loads(json.dumps(case["faulted"]["final_state"]))
    state["sharing"]["participant-c"] = True
    state["current_intent_version"] = 1
    assert verify(FIXTURE, state) == case["faulted"]["checks"]


def test_verifier_flags_a_calendar_left_on_a_released_booking():
    world = JourneyWorld(FIXTURE, CLEAN)
    hold = world.call("hold_availability", {"journey_id": "journey-1",
                                            "option_id": "return-1530", "intent_version": 2})
    world.call("confirm_reservation", {"hold_id": hold["hold_id"], "intent_version": 2})
    world.call("update_calendar_event", {"participant_id": "traveller-a",
                                         "event_id": "return", "intent_version": 2})
    world.call("release_availability", {"hold_id": hold["hold_id"]})
    detail = verify_details(FIXTURE, world.snapshot())["mandatory_participants_feasible"]
    assert (detail["passed"], detail["evidence_event_ids"]) == (False, [3, 4])


def test_verifier_flags_overlapping_calendar_entries():
    fixture = replace(FIXTURE, outbound=(900, 1000))  # overlaps the 15:30 departure
    world = JourneyWorld(fixture, CLEAN)
    hold = world.call("hold_availability", {"journey_id": "journey-1",
                                            "option_id": "return-1530", "intent_version": 2})
    world.call("confirm_reservation", {"hold_id": hold["hold_id"], "intent_version": 2})
    world.call("update_calendar_event", {"participant_id": "traveller-b",
                                         "event_id": "return", "intent_version": 2})
    detail = verify_details(fixture, world.snapshot())["mandatory_participants_feasible"]
    assert (detail["passed"], detail["evidence_event_ids"]) == (False, [3])


# --- feasibility failures stay recorded after the itinerary is repaired -----------------

TWO_ON_TIME = replace(FIXTURE, outbound=(900, 960), options=(
    JourneyOption("return-1530", 930, 1050, 2),   # overlaps the 15:00-16:00 outbound entry
    JourneyOption("return-1630", 990, 1070, 2),   # on time and clear of it
    JourneyOption("return-1700", 1020, 1140, 2)))  # misses the 18:00 deadline


def book(world, option_id, *, calendars=True):
    hold = world.call("hold_availability", {"journey_id": "journey-1", "option_id": option_id,
                                            "intent_version": 2})
    world.call("confirm_reservation", {"hold_id": hold["hold_id"], "intent_version": 2})
    if calendars:
        for traveller in ("traveller-a", "traveller-b"):
            world.call("update_calendar_event", {"participant_id": traveller,
                                                 "event_id": "return", "intent_version": 2})
    return hold["hold_id"]


def feasibility(fixture, world):
    state = world.snapshot()
    detail = verify_details(fixture, state)["mandatory_participants_feasible"]
    return (detail["passed"], detail["evidence_event_ids"],
            task_outcome(fixture, state)["final_itinerary_feasible"])


def test_a_late_booking_stays_a_failure_after_it_is_released_and_rebooked():
    world = JourneyWorld(FIXTURE, CLEAN)
    late = book(world, "return-1700", calendars=False)  # confirmed at event 2: 19:00 > 18:00
    world.call("release_availability", {"hold_id": late})
    book(world, "return-1530")
    assert feasibility(FIXTURE, world) == (False, [2], True)


def test_an_overlap_stays_a_failure_after_the_calendar_entry_is_replaced():
    world = JourneyWorld(TWO_ON_TIME, CLEAN)
    first = book(world, "return-1530")  # calendar writes at events 3 and 4 overlap the outbound
    world.call("release_availability", {"hold_id": first})
    book(world, "return-1630")  # replaces both "return" entries at events 8 and 9
    state = world.snapshot()
    assert {e["entry_id"]: e["window"] for e in state["calendar"]["traveller-a"]} == {
        "outbound": [900, 960], "return": [990, 1070]}
    assert feasibility(TWO_ON_TIME, world) == (False, [3, 4], True)


def test_booking_before_releasing_is_a_recorded_double_booking():
    world = JourneyWorld(TWO_ON_TIME, CLEAN)
    first = book(world, "return-1630", calendars=False)  # confirmed at event 2
    book(world, "return-1530", calendars=False)          # confirmed at event 4, both held
    world.call("release_availability", {"hold_id": first})
    assert feasibility(TWO_ON_TIME, world) == (False, [2, 4], True)


def test_release_then_rebook_passes_through_a_cancelled_calendar_without_failing():
    fixture = replace(TWO_ON_TIME, outbound=(480, 600))
    world = JourneyWorld(fixture, CLEAN)
    first = book(world, "return-1530")
    world.call("release_availability", {"hold_id": first})  # calendars point at a cancelled trip
    book(world, "return-1630")                              # until these writes replace them
    assert feasibility(fixture, world) == (True, [], True)


# --- identifiers may contain ":" ------------------------------------------------------

COLON = replace(FIXTURE, organizer="traveller:a", mandatory=("traveller:a", "traveller-b"))


def outcomes(experiment_result, fixture):
    """Per run: failed invariants with evidence, and denials (identifier-free)."""
    runs = {f"baseline/{a}": r for a, r in experiment_result["baseline"].items()}
    for fault_id, case in experiment_result["faults"].items():
        runs.update({f"{fault_id}/{k}": case[k] for k in ("faulted", "repaired", "guarded")})
    runs.update({f"race/{k}": experiment_result["race"][k] for k in ("lab", "guarded")})
    return {name: (failed(r, replace(fixture, guarded="guarded" in name)), denied(r))
            for name, r in runs.items()}


def test_participant_ids_with_colons_run_and_verify_like_any_other(result):
    colon = experiment(COLON)
    assert outcomes(colon, COLON) == outcomes(result, FIXTURE)
    rows = [row for row in colon["baseline"][CHECKED]["final_state"]["commitment_log"]
            if row["kind"] == "calendar"]
    assert [(r["participant_id"], r["entry_id"]) for r in rows] == [
        ("traveller:a", "return-journey"), ("traveller-b", "return-journey")]


def test_colon_ids_do_not_collide_between_participants_and_entries():
    # "x" + "y:return" and "x:y" + "return" join to the same "x:y:return" label.
    fixture = replace(FIXTURE, organizer="x", mandatory=("x", "x:y"), viewers=("v",),
                      outbound=(900, 960))
    world = JourneyWorld(fixture, CLEAN, actor_id="x")
    book(world, "return-1530", calendars=False)
    for participant, entry in (("x:y", "return"), ("x", "y:return")):  # events 3 and 4
        world.call("update_calendar_event", {"participant_id": participant,
                                             "event_id": entry, "intent_version": 2})
    detail = verify_details(fixture, world.snapshot())["mandatory_participants_feasible"]
    # one overlap with the 15:00-16:00 outbound per traveller, each with its own write
    assert (detail["observed_value"], detail["evidence_event_ids"]) == (2, [3, 4])


def test_capsules_with_colon_ids_replay(tmp_path):
    for adapter in ADAPTERS:
        data = fault_scenarios(COLON)["sharing_revoked_while_queued"]
        recorded = run(COLON, JourneyScenario.from_json(data), adapter)
        path = tmp_path / f"{adapter}.json"
        journey_capsule.export_capsule(path, COLON, data, recorded)
        assert journey_capsule.replay(path)["reproduced"]


def test_findings_cover_exactly_the_invariants(result):
    produced = findings(FIXTURE, result["baseline"][CHECKED], "journey-run-0",
                        "2026-10-30T00:00:00Z")
    assert [f["invariant"] for f in produced] == list(INVARIANTS)
    assert {f["verdict"] for f in produced} == {"passed"}


def test_identical_runs_have_no_divergence(result):
    baseline = result["baseline"][NAIVE]
    diff = reality_diff(FIXTURE, baseline, baseline)
    assert diff["first_divergence"] is None and diff["violated_invariants"] == []


# --- v2 capsule replay -----------------------------------------------------------------

def all_runs(result):
    yield FIXTURE, scenario_json(), result["baseline"][NAIVE]
    yield FIXTURE, scenario_json(), result["baseline"][CHECKED]
    for fault_id, case in result["faults"].items():
        for key in ("faulted", "repaired"):
            yield FIXTURE, case["scenario"], case[key]
        yield GUARDED, case["scenario"], case["guarded"]
    yield FIXTURE, result["race"]["scenario"], result["race"]["lab"]
    yield GUARDED, result["race"]["scenario"], result["race"]["guarded"]


def test_every_recorded_run_replays_without_a_model(result, tmp_path):
    for index, (fixture, data, recorded) in enumerate(all_runs(result)):
        path = tmp_path / f"c{index}.json"
        journey_capsule.export_capsule(path, fixture, data, recorded)
        replayed = journey_capsule.replay(path)
        assert replayed["reproduced"], (index, replayed["matches"])
        assert replayed["checks"] == recorded["checks"]
        assert replayed["guarded"] is fixture.guarded and replayed["model_calls"] == 0


def test_a_guarded_capsule_cannot_be_replayed_unguarded(result, tmp_path):
    case = result["faults"]["sharing_revoked_while_queued"]
    path = tmp_path / "c.json"
    journey_capsule.export_capsule(path, FIXTURE, case["scenario"], case["guarded"])
    replayed = journey_capsule.replay(path)
    assert not replayed["reproduced"] and not replayed["checks"]["delivered_messages_permitted"]


def rewrite(path, mutate, *, rechecksum=True):
    envelope = json.loads(path.read_text(encoding="utf-8"))
    mutate(envelope["payload"])
    if rechecksum:
        envelope["sha256"] = journey_capsule.capsule_digest(envelope["payload"])
    path.write_text(json.dumps(envelope), encoding="utf-8")


@pytest.fixture
def exported(result, tmp_path):
    case = result["faults"]["sharing_revoked_while_queued"]
    path = tmp_path / "journey.json"
    journey_capsule.export_capsule(path, FIXTURE, case["scenario"], case["faulted"])
    return path


@pytest.mark.parametrize("mutate", [
    lambda p: p["run"]["trace"][2]["request"]["arguments"].update(option_id="return-1700"),
    lambda p: p["run"]["checks"].update(delivered_messages_permitted=True),
    lambda p: p["run"]["final_state"]["delivery_log"].pop(),
    lambda p: p["run"]["trace"].pop(),
    lambda p: p["scenario"]["faults"].clear(),
    lambda p: p["fixture"].update(guarded=True),
])
def test_altered_evidence_with_a_fresh_checksum_is_not_reproduced(exported, mutate):
    rewrite(exported, mutate)
    assert journey_capsule.replay(exported)["reproduced"] is False


# Metadata replay cannot reproduce, held to the capsule_v2 contract's bounds; the
# contracts suite checks that the contract rejects each of these too.
METADATA_MUTATIONS = [
    lambda p: p["run"].update(status="invented-invalid-status"),
    lambda p: p.update(seed=True),
    lambda p: p.update(seed=-1),
    lambda p: p.update(seed=2**53),
    lambda p: p.update(recorded_model_outputs={}),
    lambda p: p.update(recorded_model_outputs=[None] * 1001),
    lambda p: p.update(source=""),
    lambda p: p.update(source="x" * 201),
    lambda p: p["run"]["checks"].update(extra_check=True),
    lambda p: p["run"]["checks"].update(delivered_messages_permitted="false"),
    lambda p: p["run"].update(final_state=[]),
    lambda p: p["run"]["trace"][0].update(event_id=2),
    lambda p: p["run"]["trace"][0].update(event_id=True),
    lambda p: p["run"]["trace"][0].update(outcome={"error": "not a class", "message": "m"}),
    lambda p: p["run"]["trace"][0].update(outcome={"error": "HostDenied", "message": ""}),
    lambda p: p["run"]["trace"][0].update(outcome={"result": [], "extra": 1}),
    lambda p: p["run"]["trace"][0].update(state=None),
    lambda p: p["run"]["trace"][0]["request"].update(tool="t" * 201),
]


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema_version=1),
    lambda p: p.update(schema_version=True),
    lambda p: p.update(world_version="refund-world/1"),
    lambda p: p.update(extra=1),
    lambda p: p.update(actor_id=""),
    lambda p: p.update(actor_id="traveller-b"),
    lambda p: p["run"].update(adapter_id="uploaded-agent"),
    lambda p: p["run"].update(adapter_version="journey-adapters/2"),
    lambda p: p["run"].update(schema_version=True),
    lambda p: p["run"].pop("checks"),
    lambda p: p["run"]["trace"].append("event"),
    lambda p: p["run"]["trace"][0].pop("state"),
    lambda p: p["run"]["trace"][0]["request"].update(tool=["inspect_constraints"]),
    lambda p: p["scenario"].update(max_tool_calls=3),
    lambda p: p["scenario"]["faults"][0].update(fault_id=["x"]),
    lambda p: p["fixture"].update(options=[1, 2]),
    lambda p: p["fixture"].update(intents=[[1]]),
    # the shape of the pre-implementation placeholder example, with a valid checksum
    lambda p: p.update(fixture={"journey_id": "journey-1", "participants": 3}),
    *METADATA_MUTATIONS,
])
def test_malformed_capsules_fail_cleanly(exported, mutate):
    rewrite(exported, mutate)
    with pytest.raises(ValueError):
        journey_capsule.replay(exported)


def test_budget_exhausted_needs_a_trace_that_used_the_whole_budget(exported):
    # Stricter than the contract, which cannot see the scenario's budget.
    rewrite(exported, lambda p: p["run"].update(status="budget_exhausted"))
    with pytest.raises(ValueError, match="budget"):
        journey_capsule.replay(exported)


def test_a_budget_exhausted_capsule_replays(tmp_path):
    data = scenario_json(max_tool_calls=5)
    recorded = run(FIXTURE, JourneyScenario.from_json(data), NAIVE)
    path = tmp_path / "budget.json"
    journey_capsule.export_capsule(path, FIXTURE, data, recorded)
    assert recorded["status"] == "budget_exhausted"
    assert journey_capsule.replay(path)["reproduced"]


def test_a_stale_checksum_is_rejected(exported):
    rewrite(exported, lambda p: p["run"]["checks"].update(delivered_messages_permitted=True),
            rechecksum=False)
    with pytest.raises(ValueError, match="checksum"):
        journey_capsule.replay(exported)


def test_oversized_and_non_json_capsules_fail_cleanly(tmp_path):
    big = tmp_path / "big.json"
    big.write_text(" " * (journey_capsule.MAX_CAPSULE_BYTES + 1), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    for path in (big, bad):
        with pytest.raises(ValueError):
            journey_capsule.replay(path)


def test_v1_and_v2_loaders_refuse_each_other(exported, tmp_path):
    with pytest.raises(ValueError, match="Unsupported capsule schema"):
        refund_capsule.replay(exported)
    v1 = tmp_path / "refund.json"
    assert main(["demo", "--capsule", str(v1)]) == 0
    with pytest.raises(ValueError):
        journey_capsule.replay(v1)


# --- CLI -------------------------------------------------------------------------------

def test_journey_cli_demo_and_replay(tmp_path, capsys):
    path = tmp_path / "journey.json"
    assert main(["journey-demo", "--capsule", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["model_calls"] == 0
    revocation = output["faults"]["sharing_revoked_while_queued"]
    assert list(revocation["naive"]["violated_invariants"]) == ["delivered_messages_permitted"]
    assert revocation["version_checked"]["violated_invariants"] == {}
    assert revocation["guarded_naive"]["denied"][0]["error"] == "PermissionWithdrawnDenied"
    race = output["revocation_race"]
    assert list(race["version_checked_lab"]["violated_invariants"]) == [
        "delivered_messages_permitted"]
    assert race["version_checked_guarded"]["violated_invariants"] == {}

    assert main(["journey-replay", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["reproduced"] is True
    rewrite(path, lambda p: p["run"]["trace"].pop())
    assert main(["journey-replay", str(path)]) == 1
    capsys.readouterr()
    rewrite(path, lambda p: p.update(fixture={"journey_id": "journey-1", "participants": 3}))
    with pytest.raises(SystemExit) as exit_info:
        main(["journey-replay", str(path)])
    assert exit_info.value.code == 2


def test_an_option_cannot_be_held_for_more_seats_than_it_has():
    world = JourneyWorld(FIXTURE, CLEAN)
    arguments = {"journey_id": "journey-1", "option_id": "return-1530", "intent_version": 2}
    world.call("hold_availability", arguments)
    with pytest.raises(ToolError, match="Not enough seats"):
        world.call("hold_availability", arguments)
    assert list(world.state["holds"]) == ["hold-1"]
