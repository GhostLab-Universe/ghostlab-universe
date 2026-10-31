"""M02 service-relay/1: world, guard, verifier, adapters, diff and v2 capsule replay.

Stdlib and pytest only (no Pydantic); contract conformance lives in
test_m02_relay_contracts.py. No __init__.py here, so the bare-interpreter core
suite is unaffected.
"""

import itertools
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ghostlab import capsule as refund_capsule
from ghostlab.cli import main
from ghostlab.execution import relay_guard
from ghostlab.execution.relay_guard import (CapacityDenied, ConfirmedLeaseDenied,
                                            DuplicateLeaseDenied, GuardedRelayWorld,
                                            OwnerTakeoverDenied, StalePromiseDenied,
                                            UnbackedConfirmationDenied, make_world)
from ghostlab.worlds import journey_capsule, relay_capsule
from ghostlab.worlds.relay import (FAULTS, READ_TOOLS, TOOL_SCHEMAS, BudgetExceeded, HostDenied,
                                   RelayCompetitor, RelayFixture, RelayJob, RelayResource,
                                   RelayScenario, RelayWorld, ReservationAckLost, ToolError,
                                   scenario_json)
from ghostlab.worlds.relay_run import (ADAPTERS, experiment, fault_scenarios, findings,
                                       reality_diff, run, takeover_race_scenario)
from ghostlab.worlds.relay_verify import (INVARIANTS, owner_at, promise_version_at, task_outcome,
                                          verify, verify_details)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "contracts"
FIXTURE = RelayFixture()
GUARDED = RelayFixture(guarded=True)
CLEAN = RelayScenario.from_json(scenario_json())
LOCAL, CHECKED = "relay-local-success", "relay-lease-checked"
PART, TECH_A, TECH_B = "part-compressor", "tech-a-0900", "tech-b-0900"
STALE, LOST, TAKEOVER = ("stale_availability_competition", "lost_reservation_ack",
                         "takeover_with_queued_confirmation")
TWO = {"competing_jobs": 2}
ALL_DENIALS = (CapacityDenied, ConfirmedLeaseDenied, DuplicateLeaseDenied, OwnerTakeoverDenied,
               StalePromiseDenied, UnbackedConfirmationDenied)


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
    return RelayScenario.from_json(scenario_json(faults, max_tool_calls))


def reserve(world, job_id, resource_id, key):
    return world.call("reserve_resource", {"job_id": job_id, "resource_id": resource_id,
                                           "request_key": key})


def confirm(world, job_id="job-1", version=1):
    return world.call("confirm_service", {"job_id": job_id, "promise_version": version})


def book(world, job_id="job-1", tech=TECH_A, *, version=1):
    """Lease what the job needs, then confirm: a correct booking in a quiet world."""
    if world.fixture.job(job_id).part is not None:
        reserve(world, job_id, world.fixture.job(job_id).part, f"{job_id}-part")
    reserve(world, job_id, tech, f"{job_id}-tech")
    return confirm(world, job_id, version)


def inspect(world, job_id="job-1"):
    return world.call("inspect_availability", {"job_id": job_id})


# --- the pack manifest describes this world -------------------------------------------

def test_pack_manifest_matches_the_world():
    manifest = json.loads((EXAMPLES / "core" / "world_pack.service_relay.json")
                          .read_text(encoding="utf-8"))
    assert manifest["world_version"] == "service-relay/1"
    assert manifest["roles"] == ["front_desk", "inventory", "dispatch"]
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
    assert set(verify(FIXTURE, RelayWorld(FIXTURE, CLEAN).snapshot())) == set(INVARIANTS)
    assert {a["adapter_id"]: a["kind"] for a in manifest["adapters"]} == {
        LOCAL: "vulnerable", CHECKED: "repaired"}
    assert set(ADAPTERS) == {LOCAL, CHECKED}


# --- the expected matrix --------------------------------------------------------------

def test_baseline_passes_for_both_adapters_and_completes_the_task(result):
    for recorded in result["baseline"].values():
        assert recorded["status"] == "completed"
        assert failed(recorded) == {} and denied(recorded) == []
        assert task_outcome(FIXTURE, recorded["final_state"]) == {
            "jobs": {"job-1": "confirmed", "job-2": "confirmed"},
            "all_jobs_confirmed": True, "handed_off": [], "leases_left_open": 0}


@pytest.mark.parametrize("fault_id, evidence, errors, guarded_errors, jobs", [
    (STALE, {"allocations_within_capacity": [2, 6],
             "confirmations_backed_by_reservations": [4, 7]}, [],
     ["CapacityDenied", "CapacityDenied", "UnbackedConfirmationDenied",
      "CapacityDenied", "CapacityDenied", "UnbackedConfirmationDenied"],
     {"job-1": "capacity_lost", "job-2": "confirmed"}),
    (LOST, {"allocations_within_capacity": [3]}, [(2, "ReservationAckLost")],
     ["ReservationAckLost", "DuplicateLeaseDenied"],
     {"job-1": "confirmed", "job-2": "confirmed"}),
    (TAKEOVER, {"no_automation_after_takeover": [4]}, [], ["OwnerTakeoverDenied"],
     {"job-1": "owner_takeover", "job-2": "confirmed"}),
])
def test_fault_matrix(result, fault_id, evidence, errors, guarded_errors, jobs):
    case = result["faults"][fault_id]
    assert failed(case["faulted"]) == evidence and denied(case["faulted"]) == errors
    assert case["diff"]["violated_invariants"] == [n for n in INVARIANTS if n in evidence]
    assert failed(case["repaired"]) == {}
    assert task_outcome(FIXTURE, case["repaired"]["final_state"])["jobs"] == jobs
    guarded = case["guarded"]
    assert failed(guarded, GUARDED) == {}
    assert [error for _, error in denied(guarded)] == guarded_errors


def test_stale_counters_sell_the_part_twice_and_the_lease_list_shows_it(result):
    case = result["faults"][STALE]
    faulted = case["faulted"]
    first = event(faulted, 1)["outcome"]["result"]
    assert first["availability_as_of"] == 0  # the counters predate the competitors
    assert {r["resource_id"]: r["available"] for r in first["resources"]} == {
        TECH_A: 1, TECH_B: 1, PART: 1}
    assert {(lease["job_id"], lease["resource_id"]) for lease in first["leases"]} == {
        ("job-3", PART), ("job-4", TECH_B)}
    assert event(faulted, 2)["request"]["arguments"]["resource_id"] == PART
    assert event(faulted, 6)["request"]["arguments"] == {
        "job_id": "job-2", "resource_id": TECH_A, "request_key": "request-3"}
    assert case["diff"]["first_divergence"]["event_id"] == 1  # the onset, not the violation
    assert case["diff"]["faulted_metrics"]["allocation_violations"] == 2
    assert case["diff"]["faulted_metrics"]["unbacked_confirmations"] == 2
    assert case["diff"]["baseline_metrics"]["allocation_violations"] == 0


def test_the_lease_checked_adapter_makes_no_promise_it_cannot_keep(result):
    repaired = result["faults"][STALE]["repaired"]
    state = repaired["final_state"]
    assert [row["job_id"] for row in state["confirmation_log"]] == ["job-2"]
    assert all(lease["job_id"] != "job-1" for lease in state["leases"].values())
    # it says so: the customer goes to a person instead of waiting on a promise
    assert [e["request"]["tool"] for e in repaired["trace"][:2]] == ["inspect_availability",
                                                                     "hand_off_to_owner"]
    assert owner_at(state, "conversation-1", 3) == "business_owner"
    assert [row["source"] for row in state["ownership_timeline"][2:]] == ["handoff"]
    assert task_outcome(FIXTURE, state)["handed_off"] == ["job-1"]
    assert verify(FIXTURE, state)["no_automation_after_takeover"] is True


def test_a_lost_acknowledgement_is_a_committed_lease(result):
    case = result["faults"][LOST]
    lost = event(case["faulted"], 2)
    assert lost["outcome"]["error"] == "ReservationAckLost"
    assert [row["kind"] for row in lost["state"]["lease_log"]] == ["granted"]  # it did commit
    assert event(case["faulted"], 3)["outcome"]["result"]["lease_id"] == "lease-2"
    assert event(case["repaired"], 3)["outcome"]["result"] == {
        "lease_id": "lease-1", "job_id": "job-1", "resource_id": PART, "status": "active",
        "replayed": True}
    keys = [e["request"]["arguments"]["request_key"] for e in case["repaired"]["trace"][1:3]]
    assert keys[0] == keys[1]


def test_the_takeover_is_the_divergence_and_the_confirmation_is_the_violation(result):
    case = result["faults"][TAKEOVER]
    assert case["diff"]["first_divergence"]["event_id"] == 4
    assert event(case["faulted"], 4)["request"]["tool"] == "confirm_service"
    assert case["diff"]["faulted_metrics"]["mutations_after_takeover"] == 1
    [finding] = [f for f in findings(FIXTURE, case["faulted"], "relay-run-1",
                                     "2026-10-30T00:00:00Z") if f["verdict"] == "failed"]
    assert finding["evidence_event_ids"] == [4]
    assert finding["unit"] == "mutations_after_takeover" and finding["observed_value"] == 1


def test_after_a_takeover_the_automation_leaves_the_job_alone(result):
    repaired = result["faults"][TAKEOVER]["repaired"]
    assert owner_at(repaired["final_state"], "conversation-1", 4) == "business_owner"
    tools = [e["request"]["tool"] for e in repaired["trace"]
             if e["request"]["arguments"].get("job_id") == "job-1"]
    assert tools == ["inspect_availability", "reserve_resource", "reserve_resource",
                     "inspect_availability"]
    assert task_outcome(FIXTURE, repaired["final_state"])["leases_left_open"] == 2  # the owner's
    assert task_outcome(FIXTURE, repaired["final_state"])["handed_off"] == []


# --- the check-then-act races: only the guard closes them -------------------------------

def test_takeover_race_gets_through_the_lab_world_and_only_the_guard_stops_it(result):
    race = result["races"]["owner_takeover"]
    takeover_after = race["scenario"]["faults"][0]["after_event"]
    lab = race["lab"]
    [confirmation] = failed(lab)["no_automation_after_takeover"]
    # the trigger is derived, and the takeover lands exactly on the confirmation event
    assert [row["from_event"] for row in lab["final_state"]["ownership_timeline"]
            if row["owner"] == "business_owner"] == [confirmation] == [takeover_after + 1]
    assert event(lab, confirmation)["request"]["tool"] == "confirm_service"
    check = event(lab, confirmation - 1)
    assert check["request"]["tool"] == "inspect_availability"
    assert check["outcome"]["result"]["conversation_owner"] == "automation"

    guarded = race["guarded"]
    assert failed(guarded, GUARDED) == {}
    assert denied(guarded) == [(confirmation, "OwnerTakeoverDenied")]
    assert [row["job_id"] for row in guarded["final_state"]["confirmation_log"]] == ["job-2"]
    assert race["diff"]["violated_invariants"] == ["no_automation_after_takeover"]


def test_capacity_race_stays_a_failure_after_the_adapter_backs_out(result):
    race = result["races"]["capacity"]
    lab = race["lab"]
    [grant] = failed(lab)["allocations_within_capacity"]
    assert event(lab, grant)["request"]["tool"] == "reserve_resource"
    assert event(lab, grant - 1)["outcome"]["result"]["leases"] == []  # it read a free part
    # the adapter noticed and released, but the oversold event stays on the record
    assert {lease["status"] for lease in lab["final_state"]["leases"].values()
            if lease["job_id"] == "job-1"} == {"released"}
    assert [row["job_id"] for row in lab["final_state"]["confirmation_log"]] == ["job-2"]
    assert task_outcome(FIXTURE, lab["final_state"])["handed_off"] == ["job-1"]
    assert failed(lab) == {"allocations_within_capacity": [grant]}
    assert denied(race["guarded"]) == [(grant, "CapacityDenied")]
    assert failed(race["guarded"], GUARDED) == {}


def test_lease_expiry_race_gets_through_the_lab_world_and_only_the_guard_stops_it(result):
    race = result["races"]["lease_expiry"]
    fixture = RelayFixture.from_json(race["fixture"])
    assert fixture.lease_ttl_events == 3  # derived: first lease at 2, confirmation at 5
    lab = race["lab"]
    [confirmation] = failed(lab, fixture)["confirmations_backed_by_reservations"]
    assert event(lab, confirmation)["request"]["tool"] == "confirm_service"
    check = event(lab, confirmation - 1)["outcome"]["result"]
    assert PART in {lease["resource_id"] for lease in check["leases"]}  # still held at the check
    expired = [row for row in lab["final_state"]["lease_log"] if row["kind"] == "expired"]
    assert [(row["event_id"], row["resource_id"]) for row in expired] == [(confirmation, PART)]
    guarded = race["guarded"]
    assert denied(guarded) == [(confirmation, "UnbackedConfirmationDenied")]
    guarded_fixture = replace(fixture, guarded=True)
    assert failed(guarded, guarded_fixture) == {}
    # refused at the last moment, it gives back what it still holds
    assert task_outcome(guarded_fixture, guarded["final_state"])["leases_left_open"] == 0


def test_a_promise_that_moves_on_the_confirmation_event_is_refused_and_backed_out_of():
    # job-4 takes tech-b at event 5: job-1's leases stay good, but its promise moves.
    faults = (STALE, 4, TWO)
    lab = run(FIXTURE, scenario(faults), CHECKED)
    assert failed(lab) == {"confirmations_backed_by_reservations": [5]}
    guarded = run(GUARDED, scenario(faults), CHECKED)
    assert denied(guarded) == [(5, "StalePromiseDenied")]
    assert failed(guarded, GUARDED) == {}
    assert {lease["status"] for lease in guarded["final_state"]["leases"].values()
            if lease["job_id"] == "job-1"} == {"released"}
    assert task_outcome(GUARDED, guarded["final_state"])["leases_left_open"] == 0


def test_race_triggers_follow_the_adapter_not_a_hardcoded_index():
    # One more job earlier in the list moves every event, and the races still land.
    fixture = replace(FIXTURE, jobs=(RelayJob("job-0", "conversation-0", None), *FIXTURE.jobs),
                      resources=(*FIXTURE.resources, RelayResource("tech-c-0900", "technician", 1)))
    assert takeover_race_scenario(fixture)["faults"][0]["after_event"] == 3
    lab = run(fixture, RelayScenario.from_json(takeover_race_scenario(fixture)), CHECKED)
    assert failed(lab, fixture) == {"no_automation_after_takeover": [4]}


def test_takeover_lands_before_the_guard_reads_ownership():
    # The race itself, on the bare world, at the exact event.
    world = make_world(GUARDED, scenario((TAKEOVER, 3, {})))
    reserve(world, "job-1", PART, "k1")
    reserve(world, "job-1", TECH_A, "k2")
    assert inspect(world)["conversation_owner"] == "automation"  # event 3: still ours
    with pytest.raises(OwnerTakeoverDenied):
        confirm(world)  # event 4: the takeover applies inside this call
    assert world.state["confirmation_log"] == []


# --- a guarded run never breaks an invariant, whatever the timing ------------------------

def sweep_cases():
    triggers = [None, *range(1, 13)]
    for adapter, after in itertools.product(ADAPTERS, triggers):
        yield adapter, ((STALE, after, {"competing_jobs": 1}),), 12
        yield adapter, ((STALE, after, TWO),), 12
        yield adapter, ((LOST, after, {}),), 12
        yield adapter, ((TAKEOVER, after, {}),), 12
        yield adapter, ((LOST, after, {}), (TAKEOVER, after, {})), 12
        yield adapter, ((STALE, after, TWO), (TAKEOVER, 3, {})), 12
    for adapter, ttl in itertools.product(ADAPTERS, range(1, 8)):
        yield adapter, ((TAKEOVER, 3, {}),), ttl
        yield adapter, ((LOST, None, {}),), ttl
        yield adapter, ((STALE, 2, TWO),), ttl


def test_no_timing_lets_a_guarded_run_violate_an_invariant():
    count = 0
    for adapter, faults, ttl in sweep_cases():
        fixture = replace(GUARDED, lease_ttl_events=ttl)
        recorded = run(fixture, scenario(*faults), adapter)
        assert failed(recorded, fixture) == {}, (adapter, faults, ttl)
        count += 1
    assert count == 2 * 13 * 6 + 2 * 7 * 3  # adapters x triggers x cases, then x lifetimes


@pytest.mark.parametrize("adapter", list(ADAPTERS))
@pytest.mark.parametrize("guarded", [False, True])
def test_both_adapters_finish_within_the_target_role_limit_under_every_timing(adapter, guarded):
    for _, faults, ttl in sweep_cases():
        fixture = replace(FIXTURE, guarded=guarded, lease_ttl_events=ttl)
        recorded = run(fixture, scenario(*faults, max_tool_calls=20), adapter)
        assert recorded["status"] == "completed", (faults, ttl)
        assert len(recorded["trace"]) <= 20


# --- the guard changes nothing it denies -----------------------------------------------

def changing(state):
    keys = ("leases", "request_keys", "promises", "conversations", "confirmed_jobs", "lease_log",
            "confirmation_log", "mutation_log", "promise_timeline", "ownership_timeline")
    return {key: state[key] for key in keys}


def denial_cases():
    def capacity(world):
        return lambda: reserve(world, "job-2", TECH_B, "k")

    def duplicate(world):
        reserve(world, "job-1", PART, "k1")
        return lambda: reserve(world, "job-1", PART, "k2")

    def stale_promise(world):
        return lambda: confirm(world, "job-1", 1)

    def unbacked(world):
        return lambda: confirm(world, "job-1", 1)

    def confirmed_lease(world):
        book(world, "job-2", TECH_A)
        return lambda: world.call("release_resource", {"lease_id": "lease-1"})

    def takeover(world):
        return lambda: reserve(world, "job-1", PART, "k")

    def takeover_release(world):
        reserve(world, "job-1", PART, "k1")
        reserve(world, "job-1", TECH_A, "k2")
        return lambda: world.call("release_resource", {"lease_id": "lease-1"})

    return [(capacity, CapacityDenied, [(STALE, None, TWO)]),
            (duplicate, DuplicateLeaseDenied, []),
            (stale_promise, StalePromiseDenied, [(STALE, 1, TWO)]),
            (unbacked, UnbackedConfirmationDenied, []),
            (confirmed_lease, ConfirmedLeaseDenied, []),
            (takeover, OwnerTakeoverDenied, [(TAKEOVER, 1, {})]),
            (takeover_release, OwnerTakeoverDenied, [(TAKEOVER, None, {})])]


@pytest.mark.parametrize("setup, error, faults", denial_cases())
def test_guard_denials_record_an_error_event_and_change_nothing(setup, error, faults):
    world = make_world(GUARDED, scenario(*faults))
    assert isinstance(world, GuardedRelayWorld)
    if faults and faults[0][1] == 1:
        inspect(world)  # the trigger needs one event behind it
    action = setup(world)
    if faults and faults[0][0] == STALE:
        inspect(world)
    before = world.snapshot()
    with pytest.raises(error):
        action()
    after = world.trace[-1]
    assert after["outcome"]["error"] == error.__name__
    if error is OwnerTakeoverDenied:  # the takeover itself lands at the denied event
        changed = {k: v for k, v in changing(after["state"]).items() if v != changing(before)[k]}
        assert set(changed) <= {"conversations", "ownership_timeline"}
    else:
        assert changing(after["state"]) == changing(before)


def test_guard_error_names_are_the_declared_denials():
    assert relay_guard.DENIALS == {cls.__name__ for cls in ALL_DENIALS}


def test_the_lab_world_permits_the_same_mistakes():
    world = make_world(FIXTURE, CLEAN)
    assert type(world) is RelayWorld
    reserve(world, "job-1", PART, "k1")
    reserve(world, "job-1", PART, "k2")     # a second lease on a one-unit part
    confirm(world, "job-1")                 # and no technician
    assert verify(FIXTURE, world.snapshot()) == {
        "allocations_within_capacity": False, "confirmations_backed_by_reservations": False,
        "no_automation_after_takeover": True}


def test_a_retry_of_a_recorded_key_is_not_a_capacity_problem_when_guarded():
    world = make_world(GUARDED, scenario((LOST, None, {})))
    with pytest.raises(ReservationAckLost):
        reserve(world, "job-1", PART, "k")
    assert reserve(world, "job-1", PART, "k")["replayed"] is True  # the part is "full": of ours
    assert len(world.state["leases"]) == 1


# --- host checks are always on ---------------------------------------------------------

@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("tool, arguments", [
    ("delete_job", {"job_id": "job-1"}),
    ("inspect_availability", {"job_id": "job-1", "actor_id": "owner-1"}),
    ("inspect_availability", {}),
    ("inspect_availability", {"job_id": "job-9"}),
    ("inspect_availability", {"job_id": "job-3"}),  # a competitor's job isn't ours to touch
    ("inspect_availability", {"job_id": ""}),
    ("inspect_availability", {"job_id": "x" * 101}),
    ("inspect_availability", {"job_id": 1}),
    ("inspect_availability", ["job-1"]),
    (["inspect_availability"], {"job_id": "job-1"}),
    ("reserve_resource", {"job_id": "job-1", "resource_id": "tech-z", "request_key": "k"}),
    ("reserve_resource", {"job_id": "job-3", "resource_id": PART, "request_key": "k"}),
    ("reserve_resource", {"job_id": "job-2", "resource_id": PART, "request_key": "k"}),
    ("reserve_resource", {"job_id": "job-1", "resource_id": PART, "request_key": ""}),
    ("reserve_resource", {"job_id": "job-1", "resource_id": PART, "request_key": "k" * 101}),
    ("release_resource", {"lease_id": "lease-9"}),
    ("confirm_service", {"job_id": "job-1", "promise_version": True}),
    ("confirm_service", {"job_id": "job-1", "promise_version": 0}),
    ("confirm_service", {"job_id": "job-1", "promise_version": 2}),  # no such version yet
    ("confirm_service", {"job_id": "job-3", "promise_version": 1}),
    ("hand_off_to_owner", {"conversation_id": "conversation-9"}),
])
def test_malformed_unknown_and_unreferenced_calls_are_denied(guarded, tool, arguments):
    world = make_world(replace(FIXTURE, guarded=guarded), CLEAN)
    before = world.snapshot()
    with pytest.raises(HostDenied):
        world.call(tool, arguments)
    assert world.trace[-1]["outcome"]["error"] == "HostDenied"
    assert world.snapshot() == before


def test_a_competitor_lease_cannot_be_released_through_the_tool():
    for fixture in (FIXTURE, GUARDED):
        world = make_world(fixture, scenario((STALE, None, TWO)))
        [lease] = [row["lease_id"] for row in inspect(world)["leases"]
                   if row["job_id"] == "job-3"]
        with pytest.raises(HostDenied, match="another job"):
            world.call("release_resource", {"lease_id": lease})
        assert world.state["leases"][lease]["status"] == "active"


@pytest.mark.parametrize("tool, arguments", [
    ("reserve_resource", {"job_id": "job-3", "resource_id": PART, "request_key": "k"}),
    ("confirm_service", {"job_id": "job-1", "promise_version": 7}),
    ("release_resource", {"lease_id": "lease-9"}),
])
def test_host_reference_checks_precede_the_guard(tool, arguments):
    world = make_world(GUARDED, CLEAN)
    with pytest.raises(HostDenied):  # a stale version on a bad reference is still HostDenied
        world.call(tool, arguments)


@pytest.mark.parametrize("actor, read_allowed", [
    ("owner-1", True), ("stranger", False)])
def test_only_the_bound_automation_may_act(actor, read_allowed):
    world = RelayWorld(FIXTURE, CLEAN, actor_id=actor)
    if read_allowed:
        inspect(world)
    else:
        with pytest.raises(HostDenied):
            inspect(world)
    for call in (lambda: reserve(world, "job-1", PART, "k"), lambda: confirm(world, "job-1"),
                 lambda: world.call("hand_off_to_owner", {"conversation_id": "conversation-1"})):
        with pytest.raises(HostDenied):
            call()
    assert world.state["leases"] == {} and world.state["mutation_log"] == []


@pytest.mark.parametrize("adapter", list(ADAPTERS))
def test_a_spoofed_principal_cannot_act_through_an_adapter(adapter):
    recorded = run(FIXTURE, CLEAN, adapter, actor_id="owner-1")
    assert recorded["actor_id"] == "owner-1"
    assert recorded["final_state"]["lease_log"] == []
    assert recorded["final_state"]["confirmation_log"] == []
    assert {error for _, error in denied(recorded)} == {"HostDenied"}


@pytest.mark.parametrize("actor_id", ["", "x" * 101, 7])
def test_invalid_principals_are_rejected(actor_id):
    with pytest.raises(ValueError):
        RelayWorld(FIXTURE, CLEAN, actor_id=actor_id)


# --- budgets, isolation and determinism -------------------------------------------------

@pytest.mark.parametrize("adapter", list(ADAPTERS))
@pytest.mark.parametrize("budget", [1, 3, 6])
def test_the_budget_is_checked_before_each_call(adapter, budget):
    recorded = run(FIXTURE, scenario(max_tool_calls=budget), adapter)
    assert recorded["status"] == "budget_exhausted"
    assert len(recorded["trace"]) == budget
    world = RelayWorld(FIXTURE, scenario(max_tool_calls=1))
    inspect(world)
    with pytest.raises(BudgetExceeded):
        inspect(world)
    assert len(world.trace) == 1


def test_runs_are_deterministic_and_branches_share_no_state(result):
    assert experiment(FIXTURE) == result
    baseline = result["baseline"][LOCAL]
    again = run(FIXTURE, CLEAN, LOCAL)
    again["final_state"]["leases"]["lease-1"]["status"] = "tampered"
    again["trace"][0]["state"]["promises"]["job-1"] = 99
    assert baseline["final_state"]["leases"]["lease-1"]["status"] == "committed"
    assert run(FIXTURE, CLEAN, LOCAL) == baseline


def test_world_results_and_snapshots_are_copies():
    world = RelayWorld(FIXTURE, CLEAN)
    seen = inspect(world)
    seen["resources"][0]["capacity"] = 99
    world.trace[0]["state"]["promises"]["job-1"] = 99
    assert world.state["promises"]["job-1"] == 1
    with pytest.raises(AttributeError):
        FIXTURE.guarded = True  # frozen


@pytest.mark.parametrize("change", [
    {"operator": "owner-1"}, {"shop_id": ""}, {"guarded": 1}, {"lease_ttl_events": 0},
    {"lease_ttl_events": True}, {"lease_ttl_events": "12"},
    {"resources": (RelayResource("t", "technician", 0),)},
    {"resources": (RelayResource("t", "van", 1),)},
    {"resources": (RelayResource("t", ["technician"], 1),)},
    {"resources": (RelayResource("t", "technician", True),)},
    {"resources": (RelayResource(PART, "part", 1),)},  # no technician at all
    {"jobs": ()},
    {"jobs": (RelayJob("job-1", "c-1", None), RelayJob("job-1", "c-2", None))},
    {"jobs": (RelayJob("job-1", "c-1", None), RelayJob("job-2", "c-1", None))},
    {"jobs": (RelayJob("job-1", "c-1", TECH_A),)},  # a technician slot isn't a part
    {"jobs": (RelayJob("job-1", "c-1", ["x"]),)},
    {"competitors": (RelayCompetitor("job-1", PART),)},  # already served
    {"competitors": (RelayCompetitor("job-9", "nothing"),)},
    {"resources": [RelayResource("t", "technician", 1)]},  # a list isn't a fixture tuple
    {"jobs": ("job-1",)},
])
def test_invalid_fixtures_are_rejected(change):
    with pytest.raises(ValueError):
        replace(FIXTURE, **change)


def test_fixture_json_round_trips():
    assert RelayFixture.from_json(GUARDED.to_json()) == GUARDED
    assert json.loads(json.dumps(FIXTURE.to_json())) == FIXTURE.to_json()


@pytest.mark.parametrize("data", [
    [], None, {}, {**FIXTURE.to_json(), "extra": 1},
    {**FIXTURE.to_json(), "jobs": 3}, {**FIXTURE.to_json(), "jobs": ["job-1"]},
    {**FIXTURE.to_json(), "resources": [[1]]},
    {**FIXTURE.to_json(), "jobs": [{"job_id": ["x"], "conversation_id": "c", "part": None}]},
    {**FIXTURE.to_json(), "competitors": [{"job_id": "j", "resource_id": {"a": 1}}]},
    {**FIXTURE.to_json(), "resources": [{"resource_id": "r", "kind": {}, "capacity": 1}]},
])
def test_malformed_fixture_json_is_a_value_error(data):
    with pytest.raises(ValueError):
        RelayFixture.from_json(data)


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(world_version="refund-world/1"),
    lambda s: s.update(schema_version=True),
    lambda s: s.update(max_tool_calls=0),
    lambda s: s.update(extra=1),
    lambda s: s["faults"].append(dict(s["faults"][0])),
    lambda s: s["faults"][0].update(fault_id="unknown_fault"),
    lambda s: s["faults"][0].update(fault_id=["x"]),
    lambda s: s["faults"][0].update(fault_id={"a": 1}),
    lambda s: s["faults"][0].update(after_event=0),
    lambda s: s["faults"][0].update(after_event=True),
    lambda s: s["faults"][0].pop("parameters"),
    lambda s: s["faults"][0]["parameters"][0].update(value="2"),
    lambda s: s["faults"][0]["parameters"][0].update(value=True),
    lambda s: s["faults"][0]["parameters"][0].update(value=0),
    lambda s: s["faults"][0]["parameters"][0].update(value=9),
    lambda s: s["faults"][0]["parameters"][0].update(name=["competing_jobs"]),
    lambda s: s["faults"][0]["parameters"].append({"name": "competing_jobs", "value": 1}),
    lambda s: s["faults"][0]["parameters"].__setitem__(0, ["competing_jobs", 1]),
    lambda s: s["faults"].__setitem__(0, "stale"),
])
def test_invalid_scenarios_are_rejected(mutate):
    data = scenario_json([(STALE, 4, TWO)])
    mutate(data)
    with pytest.raises(ValueError):
        RelayScenario.from_json(data)


def test_a_parameterless_fault_takes_no_parameters():
    data = scenario_json([(LOST, None, {})])
    data["faults"][0]["parameters"] = [{"name": "competing_jobs", "value": 1}]
    with pytest.raises(ValueError):
        RelayScenario.from_json(data)


# --- faults, in detail -----------------------------------------------------------------

def test_competitors_arrive_at_their_event_and_lose_a_resource_already_taken():
    world = RelayWorld(FIXTURE, scenario((STALE, 2, TWO)))
    inspect(world)
    reserve(world, "job-1", PART, "k1")         # the part is ours by event 2
    assert inspect(world)["availability_as_of"] == 2  # competitors arrive at event 3
    state = world.snapshot()
    grants = [(r["event_id"], r["source"], r["job_id"], r["resource_id"])
              for r in state["lease_log"]]
    assert grants == [(2, "tool", "job-1", PART), (3, "world", "job-4", TECH_B)]  # no job-3
    assert verify(FIXTURE, state)["allocations_within_capacity"] is True


def test_competing_jobs_past_the_fixture_are_simply_absent():
    world = RelayWorld(FIXTURE, scenario((STALE, None, {"competing_jobs": 8})))
    assert len(inspect(world)["leases"]) == 2


def test_a_competing_lease_moves_the_promise_of_every_job_that_relied_on_it():
    world = RelayWorld(FIXTURE, scenario((STALE, 1, {"competing_jobs": 1})))
    inspect(world)
    inspect(world)  # job-3 takes the part at event 2; only job-1 uses it
    state = world.snapshot()
    assert state["promises"] == {"job-1": 2, "job-2": 1}
    assert promise_version_at(state, "job-1", 1) == 1 and promise_version_at(state, "job-1", 2) == 2


def test_a_promise_gone_stale_is_a_failure_with_valid_leases():
    # job-4 takes tech-b at event 3, after job-1 read version 1 and took the part.
    recorded = run(FIXTURE, scenario((STALE, 2, TWO)), LOCAL)
    assert event(recorded, 4)["request"]["arguments"] == {"job_id": "job-1", "promise_version": 1}
    assert promise_version_at(recorded["final_state"], "job-1", 4) == 2
    # The frozen counters then send job-2 to tech-a at event 6, which is its own failure:
    # job-1's leases were fine, so event 4 fails on the stale promise alone.
    assert failed(recorded) == {"allocations_within_capacity": [6],
                                "confirmations_backed_by_reservations": [4, 7]}
    assert run(FIXTURE, scenario((STALE, 2, TWO)), CHECKED)["checks"] == dict.fromkeys(
        INVARIANTS, True)


def test_an_ack_is_lost_once_and_only_after_its_trigger():
    world = RelayWorld(FIXTURE, scenario((LOST, 2, {})))
    assert reserve(world, "job-1", PART, "k1")["replayed"] is False
    inspect(world)
    with pytest.raises(ReservationAckLost):
        reserve(world, "job-1", TECH_A, "k2")
    assert reserve(world, "job-2", TECH_B, "k3")["replayed"] is False


def test_a_request_key_cannot_be_reused_for_another_reservation():
    world = RelayWorld(FIXTURE, CLEAN)
    reserve(world, "job-1", PART, "k")
    with pytest.raises(ToolError, match="another reservation"):
        reserve(world, "job-1", TECH_A, "k")
    assert len(world.state["leases"]) == 1


def test_the_default_takeover_waits_for_the_whole_booking_of_the_first_job():
    world = RelayWorld(FIXTURE, scenario((TAKEOVER, None, {})))
    reserve(world, "job-2", TECH_B, "k0")
    reserve(world, "job-1", PART, "k1")
    assert inspect(world)["conversation_owner"] == "automation"
    reserve(world, "job-1", TECH_A, "k2")
    assert inspect(world)["conversation_owner"] == "business_owner"
    assert inspect(world, "job-2")["conversation_owner"] == "automation"


def test_the_default_takeover_waits_for_the_part_as_well():
    world = RelayWorld(FIXTURE, scenario((TAKEOVER, None, {})))
    reserve(world, "job-1", TECH_A, "k1")
    assert inspect(world)["conversation_owner"] == "automation"  # no part yet
    reserve(world, "job-1", PART, "k2")
    assert inspect(world)["conversation_owner"] == "business_owner"


def test_a_takeover_does_not_reach_another_conversation():
    recorded = run(FIXTURE, scenario((TAKEOVER, None, {})), LOCAL)
    assert failed(recorded) == {"no_automation_after_takeover": [4]}  # job-2's events are fine
    assert owner_at(recorded["final_state"], "conversation-2", 7) == "automation"


def test_a_lease_lapses_at_its_deadline_and_a_confirmation_stops_the_clock():
    fixture = replace(FIXTURE, lease_ttl_events=2)
    world = RelayWorld(fixture, CLEAN)
    reserve(world, "job-2", TECH_A, "k1")   # granted at 1, lapses at 3
    assert inspect(world, "job-2")["leases"]
    assert inspect(world, "job-2")["leases"] == []
    with pytest.raises(ToolError, match="not held"):
        world.call("release_resource", {"lease_id": "lease-1"})
    reserve(world, "job-2", TECH_B, "k2")
    confirm(world, "job-2")
    for _ in range(4):
        inspect(world, "job-2")
    assert world.state["leases"]["lease-2"]["status"] == "committed"
    assert verify(fixture, world.snapshot())["confirmations_backed_by_reservations"] is True


def test_a_committed_lease_keeps_its_capacity_past_its_deadline():
    fixture = replace(FIXTURE, lease_ttl_events=2)
    world = RelayWorld(fixture, CLEAN)
    reserve(world, "job-2", TECH_A, "k1")   # event 1: its deadline is event 3
    confirm(world, "job-2")                 # event 2: committed
    inspect(world, "job-2")
    inspect(world, "job-2")                 # event 4: past the deadline, and still promised
    reserve(world, "job-1", TECH_A, "k2")   # event 5: oversold
    detail = verify_details(fixture, world.snapshot())["allocations_within_capacity"]
    assert (detail["passed"], detail["evidence_event_ids"]) == (False, [5])


def test_an_expired_lease_frees_its_capacity():
    fixture = replace(FIXTURE, lease_ttl_events=1)
    world = RelayWorld(fixture, CLEAN)
    reserve(world, "job-1", PART, "k1")
    reserve(world, "job-1", PART, "k2")  # the first lapsed at this very event
    assert verify(fixture, world.snapshot())["allocations_within_capacity"] is True


def test_a_competitor_lease_does_not_lapse():
    fixture = replace(FIXTURE, lease_ttl_events=1)
    world = RelayWorld(fixture, scenario((STALE, None, TWO)))
    for _ in range(4):
        inspect(world)
    assert len(world.state["lease_log"]) == 2


def test_handing_off_stops_the_automation_from_the_next_event():
    world = RelayWorld(FIXTURE, CLEAN)
    world.call("hand_off_to_owner", {"conversation_id": "conversation-1"})
    state = world.snapshot()
    assert owner_at(state, "conversation-1", 1) == "automation"
    assert owner_at(state, "conversation-1", 2) == "business_owner"
    assert verify(FIXTURE, state)["no_automation_after_takeover"] is True
    with pytest.raises(ToolError, match="already with the owner"):
        world.call("hand_off_to_owner", {"conversation_id": "conversation-1"})
    reserve(world, "job-1", PART, "k")
    detail = verify_details(FIXTURE, world.snapshot())["no_automation_after_takeover"]
    assert (detail["passed"], detail["evidence_event_ids"]) == (False, [3])


def test_a_handed_off_conversation_is_closed_to_the_automation_when_guarded():
    world = make_world(GUARDED, CLEAN)
    world.call("hand_off_to_owner", {"conversation_id": "conversation-1"})
    with pytest.raises(OwnerTakeoverDenied):
        reserve(world, "job-1", PART, "k")
    assert verify(GUARDED, world.snapshot())["no_automation_after_takeover"] is True


def test_a_job_is_confirmed_once():
    world = RelayWorld(FIXTURE, CLEAN)
    book(world)
    with pytest.raises(ToolError, match="already confirmed"):
        confirm(world)
    assert len(world.state["confirmation_log"]) == 1


def test_a_lease_that_lapsed_before_the_re_read_stops_the_confirmation():
    fixture = replace(FIXTURE, lease_ttl_events=1)  # gone by the next event, every time
    recorded = run(fixture, CLEAN, CHECKED)
    assert failed(recorded, fixture) == {}
    assert recorded["final_state"]["confirmation_log"] == []
    outcome = task_outcome(fixture, recorded["final_state"])
    assert outcome["handed_off"] == ["job-1", "job-2"]
    assert set(outcome["jobs"].values()) == {"unresolved"}  # handing everything off isn't done


def test_a_lease_can_be_released_and_taken_again():
    world = RelayWorld(FIXTURE, CLEAN)
    reserve(world, "job-1", PART, "k1")
    world.call("release_resource", {"lease_id": "lease-1"})
    reserve(world, "job-1", PART, "k2")
    assert verify(FIXTURE, world.snapshot())["allocations_within_capacity"] is True
    with pytest.raises(ToolError, match="not held"):
        world.call("release_resource", {"lease_id": "lease-1"})


# --- the verifier reads evidence, not live fields ---------------------------------------

@pytest.mark.parametrize("fault_id", list(FAULTS))
def test_verifier_ignores_the_live_fields_the_guard_reads(result, fault_id):
    faulted = result["faults"][fault_id]["faulted"]
    state = json.loads(json.dumps(faulted["final_state"]))
    state["conversations"] = {key: "automation" for key in state["conversations"]}
    state["promises"] = {key: 1 for key in state["promises"]}
    state["leases"] = {}
    state["request_keys"] = {}
    state["confirmed_jobs"] = []
    assert verify(FIXTURE, state) == faulted["checks"]


def test_verifier_ignores_the_live_promise_on_a_stale_confirmation():
    recorded = run(FIXTURE, scenario((STALE, 2, TWO)), LOCAL)
    state = json.loads(json.dumps(recorded["final_state"]))
    state["promises"] = {key: 1 for key in state["promises"]}
    assert verify_details(FIXTURE, state) == verify_details(FIXTURE, recorded["final_state"])


def test_verifier_uses_the_grant_deadline_not_the_worlds_expiry_mark():
    race = experiment(FIXTURE)["races"]["lease_expiry"]
    fixture = RelayFixture.from_json(race["fixture"])
    state = json.loads(json.dumps(race["lab"]["final_state"]))
    state["lease_log"] = [row for row in state["lease_log"] if row["kind"] != "expired"]
    assert verify(fixture, state)["confirmations_backed_by_reservations"] is False


def test_verifier_cites_the_grant_not_a_later_state():
    world = RelayWorld(FIXTURE, CLEAN)
    reserve(world, "job-1", PART, "k1")
    reserve(world, "job-1", PART, "k2")     # event 2: a duplicate, over capacity
    world.call("release_resource", {"lease_id": "lease-2"})
    world.call("release_resource", {"lease_id": "lease-1"})
    detail = verify_details(FIXTURE, world.snapshot())["allocations_within_capacity"]
    assert (detail["passed"], detail["evidence_event_ids"], detail["observed_value"]) == (
        False, [2], 1)


def test_a_duplicate_lease_fails_even_with_capacity_to_spare():
    fixture = replace(FIXTURE, competitors=(), resources=(
        RelayResource(TECH_A, "technician", 2), RelayResource(PART, "part", 1)))
    world = RelayWorld(fixture, CLEAN)
    reserve(world, "job-2", TECH_A, "k1")
    reserve(world, "job-2", TECH_A, "k2")
    detail = verify_details(fixture, world.snapshot())["allocations_within_capacity"]
    assert (detail["passed"], detail["evidence_event_ids"]) == (False, [2])


def test_two_jobs_may_share_a_resource_with_capacity_for_both():
    fixture = replace(FIXTURE, competitors=(), resources=(
        RelayResource(TECH_A, "technician", 2), RelayResource(PART, "part", 1)))
    world = RelayWorld(fixture, CLEAN)
    reserve(world, "job-1", TECH_A, "k1")
    reserve(world, "job-2", TECH_A, "k2")
    assert verify(fixture, world.snapshot())["allocations_within_capacity"] is True


def test_a_lease_taken_after_the_confirmation_does_not_excuse_it():
    # The customer was told at event 2 with no part leased; nothing later changes that.
    world = RelayWorld(FIXTURE, CLEAN)
    reserve(world, "job-1", TECH_A, "k1")
    confirm(world)
    reserve(world, "job-1", PART, "k2")
    detail = verify_details(FIXTURE, world.snapshot())["confirmations_backed_by_reservations"]
    assert (detail["passed"], detail["evidence_event_ids"]) == (False, [2])


def test_releasing_a_confirmed_lease_undoes_the_backing_at_the_end():
    world = RelayWorld(FIXTURE, CLEAN)
    book(world)                                             # events 1-3
    world.call("release_resource", {"lease_id": "lease-1"})  # event 4: the part goes
    detail = verify_details(FIXTURE, world.snapshot())["confirmations_backed_by_reservations"]
    assert (detail["passed"], detail["evidence_event_ids"]) == (False, [3, 4])


def swap_after_confirming(world):
    reserve(world, "job-2", TECH_A, "k1")   # event 1
    confirm(world, "job-2")                 # event 2: committed
    reserve(world, "job-2", TECH_B, "k2")   # event 3: live, but never committed
    world.call("release_resource", {"lease_id": "lease-1"})  # event 4


@pytest.mark.parametrize("reads_after", [0, 1, 11, 30])
def test_releasing_a_committed_lease_fails_at_its_event_however_long_the_run(reads_after):
    world = RelayWorld(FIXTURE, CLEAN)
    swap_after_confirming(world)
    for _ in range(reads_after):
        inspect(world)
    detail = verify_details(FIXTURE, world.snapshot())["confirmations_backed_by_reservations"]
    assert (detail["passed"], detail["evidence_event_ids"]) == (False, [2, 4])


def test_the_verifier_is_as_strict_as_the_guard_about_a_committed_lease():
    world = make_world(GUARDED, CLEAN)
    reserve(world, "job-2", TECH_A, "k1")
    confirm(world, "job-2")
    reserve(world, "job-2", TECH_B, "k2")
    with pytest.raises(ConfirmedLeaseDenied):
        world.call("release_resource", {"lease_id": "lease-1"})


def test_a_spare_committed_lease_may_be_released():
    fixture = replace(FIXTURE, competitors=())
    world = RelayWorld(fixture, CLEAN)
    reserve(world, "job-2", TECH_A, "k1")
    reserve(world, "job-2", TECH_B, "k2")   # both are live when the confirmation commits them
    confirm(world, "job-2")
    world.call("release_resource", {"lease_id": "lease-1"})
    assert verify(fixture, world.snapshot())["confirmations_backed_by_reservations"] is True
    world.call("release_resource", {"lease_id": "lease-2"})  # the last one is not spare
    detail = verify_details(fixture, world.snapshot())["confirmations_backed_by_reservations"]
    assert (detail["passed"], detail["evidence_event_ids"]) == (False, [3, 5])


def test_a_confirmation_cannot_lean_on_an_oversold_lease():
    world = RelayWorld(FIXTURE, scenario((STALE, None, TWO)))
    reserve(world, "job-1", PART, "k1")     # the competitor holds the only part
    reserve(world, "job-1", TECH_A, "k2")
    confirm(world, "job-1", world.state["promises"]["job-1"])  # the current version, even
    detail = verify_details(FIXTURE, world.snapshot())["confirmations_backed_by_reservations"]
    assert (detail["passed"], detail["evidence_event_ids"]) == (False, [3])


def test_a_mutation_refused_after_a_takeover_is_not_a_violation():
    world = RelayWorld(FIXTURE, scenario((TAKEOVER, 1, {})))
    inspect(world)
    with pytest.raises(ToolError, match="already with the owner"):
        world.call("hand_off_to_owner", {"conversation_id": "conversation-1"})
    assert world.state["mutation_log"] == []
    assert verify(FIXTURE, world.snapshot())["no_automation_after_takeover"] is True


def test_every_kind_of_mutation_after_a_takeover_counts():
    world = RelayWorld(FIXTURE, scenario((TAKEOVER, 1, {})))
    reserve(world, "job-1", PART, "k1")     # event 1: before the takeover
    reserve(world, "job-1", TECH_A, "k2")   # 2
    world.call("release_resource", {"lease_id": "lease-2"})  # 3
    confirm(world)                          # 4
    detail = verify_details(FIXTURE, world.snapshot())["no_automation_after_takeover"]
    assert (detail["observed_value"], detail["evidence_event_ids"]) == (3, [2, 3, 4])


def test_a_later_ownership_row_ends_the_takeover_for_the_verifier():
    # No tool resumes automation in this version; the rule still reads the timeline.
    world = RelayWorld(FIXTURE, scenario((TAKEOVER, 1, {})))
    inspect(world)
    reserve(world, "job-1", PART, "k")      # event 2: under the owner
    reserve(world, "job-1", TECH_A, "k2")   # event 3
    state = world.snapshot()
    state["ownership_timeline"].append({"from_event": 3, "conversation_id": "conversation-1",
                                        "owner": "automation", "source": "resumed"})
    assert verify_details(FIXTURE, state)["no_automation_after_takeover"][
        "evidence_event_ids"] == [2]


# --- the task is a separate measure -----------------------------------------------------

def test_a_job_nobody_tried_is_unresolved_not_excused():
    world = RelayWorld(FIXTURE, CLEAN)
    inspect(world)
    assert task_outcome(FIXTURE, world.snapshot())["jobs"] == {"job-1": "unresolved",
                                                               "job-2": "unresolved"}


def test_a_competitor_with_only_some_capacity_excuses_nothing():
    world = RelayWorld(FIXTURE, scenario((STALE, None, {"competing_jobs": 1})))
    inspect(world)  # job-3 took the part; job-2 needs no part, and technicians are free
    assert task_outcome(FIXTURE, world.snapshot())["jobs"] == {"job-1": "capacity_lost",
                                                               "job-2": "unresolved"}


TWO_FOR_ONE_PART = replace(FIXTURE, competitors=(), jobs=(
    RelayJob("job-1", "conversation-1", PART), RelayJob("job-2", "conversation-2", PART)))
ONE_TECHNICIAN = replace(FIXTURE, competitors=(), resources=(
    RelayResource(TECH_A, "technician", 1), RelayResource(PART, "part", 1)))


def test_jobs_trading_a_lease_that_lapses_do_not_excuse_each_other():
    fixture = replace(TWO_FOR_ONE_PART, lease_ttl_events=1)
    world = RelayWorld(fixture, CLEAN)
    reserve(world, "job-1", PART, "k1")     # job-2 could not have had it here
    reserve(world, "job-2", PART, "k2")     # job-1's lapsed; job-1 could not have had it here
    inspect(world)
    assert set(task_outcome(fixture, world.snapshot())["jobs"].values()) == {"unresolved"}


def test_a_lease_given_back_excuses_nobody():
    world = RelayWorld(ONE_TECHNICIAN, CLEAN)
    reserve(world, "job-1", TECH_A, "k1")
    world.call("release_resource", {"lease_id": "lease-1"})
    assert task_outcome(ONE_TECHNICIAN, world.snapshot())["jobs"]["job-2"] == "unresolved"


def test_capacity_a_confirmed_job_kept_is_capacity_lost():
    world = RelayWorld(ONE_TECHNICIAN, CLEAN)
    book(world)                             # job-1 keeps the only technician
    assert task_outcome(ONE_TECHNICIAN, world.snapshot())["jobs"] == {
        "job-1": "confirmed", "job-2": "capacity_lost"}


def test_one_taken_technician_is_not_capacity_lost():
    fixture = replace(FIXTURE, competitors=(RelayCompetitor("job-4", TECH_B),))
    world = RelayWorld(fixture, scenario((STALE, None, {"competing_jobs": 1})))
    inspect(world)  # tech-a is still free for either job
    assert set(task_outcome(fixture, world.snapshot())["jobs"].values()) == {"unresolved"}


def test_the_automations_own_stranded_lease_excuses_nothing(result):
    guarded = result["faults"][STALE]["guarded"]
    assert {lease["resource_id"]: lease["job_id"]
            for lease in guarded["final_state"]["leases"].values()}[TECH_A] == "job-1"
    assert task_outcome(GUARDED, guarded["final_state"])["jobs"] == {
        "job-1": "capacity_lost", "job-2": "unresolved"}


@pytest.mark.parametrize("fault_id", [STALE, LOST])
def test_a_promise_that_did_not_hold_is_not_a_confirmed_job(result, fault_id):
    state = result["faults"][fault_id]["faulted"]["final_state"]
    outcome = task_outcome(FIXTURE, state)
    held = not [key for key in outcome["jobs"].values() if key == "unbacked"]
    assert outcome["all_jobs_confirmed"] is held
    assert verify(FIXTURE, state)["confirmations_backed_by_reservations"] is held


def test_a_hand_off_is_not_an_excuse_by_itself():
    world = RelayWorld(FIXTURE, CLEAN)
    world.call("hand_off_to_owner", {"conversation_id": "conversation-1"})
    outcome = task_outcome(FIXTURE, world.snapshot())
    assert outcome["jobs"]["job-1"] == "unresolved" and outcome["handed_off"] == ["job-1"]


def test_open_leases_are_counted_and_confirmed_ones_are_not():
    world = RelayWorld(FIXTURE, CLEAN)
    book(world)
    reserve(world, "job-2", TECH_B, "k")
    assert task_outcome(FIXTURE, world.snapshot())["leases_left_open"] == 1


# --- identifiers may contain ":" ------------------------------------------------------

COLON = replace(
    FIXTURE, operator="relay:automation", business_owner="owner:1",
    resources=(RelayResource("tech:a", "technician", 1), RelayResource("tech:b", "technician", 1),
               RelayResource("part:compressor", "part", 1)),
    jobs=(RelayJob("job:1", "conversation:1", "part:compressor"),
          RelayJob("job:2", "conversation:2", None)),
    competitors=(RelayCompetitor("job:3", "part:compressor"), RelayCompetitor("job:4", "tech:b")))


def outcomes(experiment_result, fixture):
    """Per run: failed invariants with evidence, and denials (identifier-free)."""
    runs = {f"baseline/{a}": (r, fixture) for a, r in experiment_result["baseline"].items()}
    for fault_id, case in experiment_result["faults"].items():
        runs.update({f"{fault_id}/{k}": (case[k], fixture) for k in ("faulted", "repaired")})
        runs[f"{fault_id}/guarded"] = (case["guarded"], replace(fixture, guarded=True))
    for name, race in experiment_result["races"].items():
        lab = RelayFixture.from_json(race["fixture"])
        runs[f"{name}/lab"] = (race["lab"], lab)
        runs[f"{name}/guarded"] = (race["guarded"], replace(lab, guarded=True))
    return {name: (failed(r, f), denied(r)) for name, (r, f) in runs.items()}


def test_identifiers_with_colons_run_and_verify_like_any_other(result):
    colon = experiment(COLON)
    assert outcomes(colon, COLON) == outcomes(result, FIXTURE)
    state = colon["baseline"][CHECKED]["final_state"]
    assert {(row["job_id"], row["resource_id"]) for row in state["lease_log"]} == {
        ("job:1", "part:compressor"), ("job:1", "tech:a"), ("job:2", "tech:b")}


def test_colon_ids_do_not_collide_between_jobs_and_resources():
    # "a" + "b:c" and "a:b" + "c" would join to the same label.
    fixture = replace(
        FIXTURE, jobs=(RelayJob("a", "c1", "b:c"), RelayJob("a:b", "c2", "c")),
        resources=(RelayResource("t", "technician", 2), RelayResource("b:c", "part", 1),
                   RelayResource("c", "part", 1)), competitors=())
    world = RelayWorld(fixture, CLEAN)
    reserve(world, "a", "b:c", "k1")
    reserve(world, "a:b", "c", "k2")
    assert verify(fixture, world.snapshot())["allocations_within_capacity"] is True


def test_capsules_with_colon_ids_replay(tmp_path):
    for adapter in ADAPTERS:
        data = fault_scenarios(COLON)[TAKEOVER]
        recorded = run(COLON, RelayScenario.from_json(data), adapter)
        path = tmp_path / f"{adapter}.json"
        relay_capsule.export_capsule(path, COLON, data, recorded)
        assert relay_capsule.replay(path)["reproduced"]


@pytest.mark.parametrize("fixture", [
    replace(FIXTURE, competitors=()),
    replace(FIXTURE, lease_ttl_events=1),
    replace(FIXTURE, lease_ttl_events=2),
    replace(FIXTURE, jobs=(RelayJob("job-1", "conversation-1", None),)),
    replace(FIXTURE, resources=(RelayResource(TECH_A, "technician", 2),
                                RelayResource(TECH_B, "technician", 1),
                                RelayResource(PART, "part", 3))),
], ids=["no-competitors", "ttl-1", "ttl-2", "one-job", "spare-capacity"])
def test_experiment_runs_on_any_valid_fixture(fixture):
    produced = experiment(fixture)
    assert set(produced["faults"]) == set(FAULTS)
    assert set(produced["races"]) <= {"owner_takeover", "capacity", "lease_expiry"}


def test_a_fixture_whose_clean_run_never_confirms_has_no_confirmation_race():
    assert "owner_takeover" not in experiment(replace(FIXTURE, lease_ttl_events=1))["races"]


def test_every_reality_diff_compares_runs_from_one_fixture(result):
    race = result["races"]["lease_expiry"]
    assert "diff" not in race
    assert {"owner_takeover", "capacity"} == {
        name for name, case in result["races"].items() if "diff" in case}


def test_findings_cover_exactly_the_invariants(result):
    produced = findings(FIXTURE, result["baseline"][CHECKED], "relay-run-0",
                        "2026-10-30T00:00:00Z")
    assert [f["invariant"] for f in produced] == list(INVARIANTS)
    assert {f["verdict"] for f in produced} == {"passed"}


def test_identical_runs_have_no_divergence(result):
    baseline = result["baseline"][LOCAL]
    diff = reality_diff(FIXTURE, baseline, baseline)
    assert diff["first_divergence"] is None and diff["violated_invariants"] == []


# --- v2 capsule replay -----------------------------------------------------------------

def all_runs(result):
    yield FIXTURE, scenario_json(), result["baseline"][LOCAL]
    yield FIXTURE, scenario_json(), result["baseline"][CHECKED]
    for case in result["faults"].values():
        for key in ("faulted", "repaired"):
            yield FIXTURE, case["scenario"], case[key]
        yield GUARDED, case["scenario"], case["guarded"]
    for race in result["races"].values():
        lab = RelayFixture.from_json(race["fixture"])
        yield lab, race["scenario"], race["lab"]
        yield replace(lab, guarded=True), race["scenario"], race["guarded"]


def test_every_recorded_run_replays_without_a_model(result, tmp_path):
    for index, (fixture, data, recorded) in enumerate(all_runs(result)):
        path = tmp_path / f"c{index}.json"
        relay_capsule.export_capsule(path, fixture, data, recorded)
        replayed = relay_capsule.replay(path)
        assert replayed["reproduced"], (index, replayed["matches"])
        assert replayed["checks"] == recorded["checks"]
        assert replayed["guarded"] is fixture.guarded and replayed["model_calls"] == 0


def test_a_guarded_capsule_cannot_be_replayed_unguarded(result, tmp_path):
    race = result["races"]["owner_takeover"]
    path = tmp_path / "c.json"
    relay_capsule.export_capsule(path, FIXTURE, race["scenario"], race["guarded"])
    replayed = relay_capsule.replay(path)
    assert not replayed["reproduced"] and not replayed["checks"]["no_automation_after_takeover"]


def rewrite(path, mutate, *, rechecksum=True):
    envelope = json.loads(path.read_text(encoding="utf-8"))
    mutate(envelope["payload"])
    if rechecksum:
        envelope["sha256"] = relay_capsule.capsule_digest(envelope["payload"])
    path.write_text(json.dumps(envelope), encoding="utf-8")


@pytest.fixture
def exported(result, tmp_path):
    case = result["faults"][TAKEOVER]
    path = tmp_path / "relay.json"
    relay_capsule.export_capsule(path, FIXTURE, case["scenario"], case["faulted"])
    return path


@pytest.mark.parametrize("mutate", [
    lambda p: p["run"]["trace"][1]["request"]["arguments"].update(resource_id=TECH_B),
    lambda p: p["run"]["checks"].update(no_automation_after_takeover=True),
    lambda p: p["run"]["final_state"]["mutation_log"].pop(),
    lambda p: p["run"]["final_state"]["ownership_timeline"].pop(),
    lambda p: p["run"]["final_state"]["lease_log"].pop(),
    lambda p: p["run"]["final_state"]["confirmation_log"][0].update(promise_version=1.0),
    lambda p: p["run"]["final_state"]["promises"].update({"job-1": True}),
    lambda p: p["run"]["trace"][3]["outcome"]["result"].update(promise_version=1.0),
    lambda p: p["run"]["trace"].pop(),
    lambda p: p["run"]["trace"][3]["outcome"]["result"].update(status="declined"),
    lambda p: p["run"]["trace"][0]["state"].update(shop_id="another-shop"),
    lambda p: p["scenario"]["faults"].clear(),
    lambda p: p["fixture"].update(guarded=True),
    lambda p: p["fixture"].update(lease_ttl_events=1),
])
def test_altered_evidence_with_a_fresh_checksum_is_not_reproduced(exported, mutate):
    rewrite(exported, mutate)
    assert relay_capsule.replay(exported)["reproduced"] is False


# Metadata replay cannot reproduce, held to the capsule_v2 contract's bounds; the
# contracts suite checks that the contract rejects each of these too.
METADATA_MUTATIONS = [
    lambda p: p["run"].update(status="invented-invalid-status"),
    lambda p: p["run"].update(status=[]),
    lambda p: p["run"].update(status={}),
    lambda p: p["run"].update(adapter_id=[]),
    lambda p: p["run"].update(adapter_id={}),
    lambda p: p.update(source=["x"]),
    lambda p: p.update(source={"a": 1}),
    lambda p: p.update(seed=True),
    lambda p: p.update(seed=-1),
    lambda p: p.update(seed=2**53),
    lambda p: p.update(seed="0"),
    lambda p: p.update(recorded_model_outputs={}),
    lambda p: p.update(recorded_model_outputs=[None] * 1001),
    lambda p: p.update(source=""),
    lambda p: p.update(source="x" * 201),
    lambda p: p["run"]["checks"].update(extra_check=True),
    lambda p: p["run"]["checks"].update(no_automation_after_takeover="false"),
    lambda p: p["run"].update(final_state=[]),
    lambda p: p["run"].update(checks=[]),
    lambda p: p["run"]["trace"][0].update(event_id=2),
    lambda p: p["run"]["trace"][0].update(event_id=True),
    lambda p: p["run"]["trace"][0].update(outcome={"error": "not a class", "message": "m"}),
    lambda p: p["run"]["trace"][0].update(outcome={"error": "HostDenied", "message": ""}),
    lambda p: p["run"]["trace"][0].update(outcome={"error": ["x"], "message": "m"}),
    lambda p: p["run"]["trace"][0].update(outcome={"result": [], "extra": 1}),
    lambda p: p["run"]["trace"][0].update(state=None),
    lambda p: p["run"]["trace"][0]["request"].update(tool="t" * 201),
    lambda p: p["run"]["trace"][0]["request"].update(tool={"a": 1}),
    lambda p: p["run"]["trace"][0]["request"].update(arguments=["job-1"]),
]


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema_version=1),
    lambda p: p.update(schema_version=True),
    lambda p: p.update(world_version="refund-world/1"),
    lambda p: p.update(world_version=["service-relay/1"]),
    lambda p: p.update(extra=1),
    lambda p: p.update(actor_id=""),
    lambda p: p.update(actor_id=["relay-automation"]),
    lambda p: p.update(actor_id={"a": 1}),
    lambda p: p.update(actor_id="owner-1"),
    lambda p: p["run"].update(adapter_id="uploaded-agent"),
    lambda p: p["run"].update(adapter_version="relay-adapters/2"),
    lambda p: p["run"].update(adapter_version=["relay-adapters/1"]),
    lambda p: p["run"].update(schema_version=True),
    lambda p: p["run"].pop("checks"),
    lambda p: p["run"]["trace"].append("event"),
    lambda p: p["run"]["trace"][0].pop("state"),
    lambda p: p["run"]["trace"][0]["request"].update(tool=["inspect_availability"]),
    lambda p: p["scenario"].update(max_tool_calls=3),
    lambda p: p["scenario"]["faults"][0].update(fault_id=["x"]),
    lambda p: p["scenario"]["faults"][0].update(fault_id={"a": 1}),
    lambda p: p["scenario"].update(faults={}),
    lambda p: p["fixture"].update(jobs=[1, 2]),
    lambda p: p["fixture"].update(resources=[["x"]]),
    lambda p: p["fixture"].update(jobs={"job-1": 1}),
    lambda p: p["fixture"].update(guarded="false"),
    # the shape of a placeholder fixture, with a valid checksum
    lambda p: p.update(fixture={"shop_id": "shop-1", "jobs": 3}),
    *METADATA_MUTATIONS,
])
def test_malformed_capsules_fail_cleanly(exported, mutate):
    rewrite(exported, mutate)
    with pytest.raises(ValueError):
        relay_capsule.replay(exported)


@pytest.mark.parametrize("actor_id", ["", "x" * 101, 7, ["relay-automation"], {"a": 1}, None])
def test_an_invalid_principal_fails_even_when_the_capsule_agrees_with_itself(exported, actor_id):
    def mutate(payload):
        payload["actor_id"] = payload["run"]["actor_id"] = actor_id
    rewrite(exported, mutate)
    with pytest.raises(ValueError):
        relay_capsule.replay(exported)


def test_budget_exhausted_needs_a_trace_that_used_the_whole_budget(exported):
    # Stricter than the contract, which cannot see the scenario's budget.
    rewrite(exported, lambda p: p["run"].update(status="budget_exhausted"))
    with pytest.raises(ValueError, match="budget"):
        relay_capsule.replay(exported)


def test_a_budget_exhausted_capsule_replays(tmp_path):
    data = scenario_json(max_tool_calls=5)
    recorded = run(FIXTURE, RelayScenario.from_json(data), LOCAL)
    path = tmp_path / "budget.json"
    relay_capsule.export_capsule(path, FIXTURE, data, recorded)
    assert recorded["status"] == "budget_exhausted"
    assert relay_capsule.replay(path)["reproduced"]


def test_a_stale_checksum_is_rejected(exported):
    rewrite(exported, lambda p: p["run"]["checks"].update(no_automation_after_takeover=True),
            rechecksum=False)
    with pytest.raises(ValueError, match="checksum"):
        relay_capsule.replay(exported)


def test_oversized_and_non_json_capsules_fail_cleanly(tmp_path):
    big = tmp_path / "big.json"
    big.write_text(" " * (relay_capsule.MAX_CAPSULE_BYTES + 1) + "{}", encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        relay_capsule.replay(big)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        relay_capsule.replay(bad)


def test_other_loaders_refuse_this_capsule_and_this_loader_refuses_theirs(exported, tmp_path):
    with pytest.raises(ValueError, match="Unsupported capsule schema"):
        refund_capsule.replay(exported)
    with pytest.raises(ValueError, match="Unsupported world version"):
        journey_capsule.replay(exported)
    v1 = tmp_path / "refund.json"
    journey = tmp_path / "journey.json"
    assert main(["demo", "--capsule", str(v1)]) == 0
    assert main(["journey-demo", "--capsule", str(journey)]) == 0
    for path in (v1, journey):
        with pytest.raises(ValueError):
            relay_capsule.replay(path)


# --- CLI -------------------------------------------------------------------------------

def test_relay_cli_demo_and_replay(tmp_path, capsys):
    path = tmp_path / "relay.json"
    assert main(["relay-demo", "--capsule", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["model_calls"] == 0
    takeover = output["faults"][TAKEOVER]
    assert list(takeover["local_success"]["violated_invariants"]) == [
        "no_automation_after_takeover"]
    assert takeover["lease_checked"]["violated_invariants"] == {}
    assert takeover["guarded_local_success"]["denied"][0]["error"] == "OwnerTakeoverDenied"
    race = output["races"]["owner_takeover"]
    assert list(race["lease_checked_lab"]["violated_invariants"]) == [
        "no_automation_after_takeover"]
    assert race["lease_checked_guarded"]["violated_invariants"] == {}
    assert set(output["races"]) == {"owner_takeover", "capacity", "lease_expiry"}

    assert main(["relay-replay", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["reproduced"] is True
    rewrite(path, lambda p: p["run"]["trace"].pop())
    assert main(["relay-replay", str(path)]) == 1
    capsys.readouterr()
    rewrite(path, lambda p: p.update(fixture={"shop_id": "shop-1", "jobs": 3}))
    with pytest.raises(SystemExit) as exit_info:
        main(["relay-replay", str(path)])
    assert exit_info.value.code == 2
