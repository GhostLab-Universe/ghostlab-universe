"""Relay campaigns end to end: real SQLite stores, real worlds, mocked inference."""

import json
import socket
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace

import pytest

from ghostlab.contracts.world import (CapsuleV2, WorldCampaignRecord, WorldInvariantFinding,
                                      WorldMemoryQuery, WorldRunResult)
from ghostlab.orchestration import (DEFAULT_WORLDS, CampaignSettingsChanged, RelayDriver,
                                    TrustedSession, execute_relay_run, ledger_key)
from ghostlab.orchestration.relay_engine import RelayToolDenied
from ghostlab.policy import (ENFORCED, LAB, ApprovalDenied, PolicyDenied, StaticWorldBinding,
                             supervisor_scope)
from ghostlab.storage import NotFound
from ghostlab.worlds.relay import RelayFixture, RelayJob, RelayScenario, ToolError, scenario_json

from m05_harness import OPERATOR, PROJECT, STRANGER, Script, meta_reply, proposal, spec
from m05_journey_harness import J, journey_reply, journey_spec
from m05_relay_harness import (AUTOMATION, BUSINESS_OWNER, CAPACITY_RACE, CHECKED, COMPETITION,
                               FIRST_CONFIRM_EVENT, FIRST_LEASE_EVENT, LOCAL, LOST_ACK, R,
                               TAKEOVER_RACE, RelayLab, proposal_json, relay_reply, relay_spec)

CID = "campaign-1"
INVARIANT = "no_automation_after_takeover"
DRIVER = RelayDriver()


def start(tmp_path, *replies, campaign=CID, **options):
    lab = RelayLab(tmp_path, Script(*(replies or [relay_reply()])), **options)
    with lab.controller() as controller:
        return lab, controller.start(campaign, relay_spec())


def only_experiment(state):
    (experiment,) = state["experiments"].values()
    return experiment


def failed(state):
    return [f for f in state["findings"] if f["verdict"] == "failed"]


def load(lab, ref):
    view = lab.artifacts.for_project(supervisor_scope(PROJECT, OPERATOR))
    return json.loads(view.read(view.ref(ref["id"])))


def resume_after_approval(lab, state):
    lab.approve(state)
    with lab.controller() as controller:
        return controller.resume(CID)


def memory_query(project=PROJECT, world=R):
    return WorldMemoryQuery.model_validate({
        "schema_version": 1, "project_id": project, "campaign_id": None, "world_version": world,
        "kinds": ["finding"], "statuses": ["verified"], "as_of": "2026-10-29T13:00:00Z",
        "limit": 20})


def open_reservations(lab, project=PROJECT, campaign=CID):
    with closing(sqlite3.connect(lab.paths.app_db)) as conn:
        return conn.execute("SELECT reservation_id FROM budget_reservations "
                            "WHERE campaign_id = ? AND status = 'reserved'",
                            (ledger_key(project, campaign),)).fetchall()


# --- 1. the lab experiment detects the failure and stops for a human ---------------------

def test_lab_experiment_detects_a_change_after_takeover_and_pauses_for_approval(tmp_path):
    lab, state = start(tmp_path)
    assert state["paused"] and state["status"] == "awaiting_approval"
    experiment = only_experiment(state)
    assert experiment["execution"] == {"mode": "lab", "guarded": False}
    (finding,) = failed(state)
    assert (finding["invariant"], finding["evidence_event_ids"]) == (INVARIANT, [4])
    WorldInvariantFinding.model_validate(finding)
    # the evidence is the confirmation itself, in the stored run, not a claim
    fork = load(lab, experiment["fork"])
    WorldRunResult.model_validate(fork)
    assert fork["trace"][3]["request"] == {"tool": "confirm_service",
                                           "arguments": {"job_id": "job-1", "promise_version": 1}}
    assert fork["final_state"]["conversations"]["conversation-1"] == "business_owner"
    # nothing runs until a human decides
    assert state["repair"] is None
    approval = lab.approval_store.get(PROJECT, state["approval"]["approval_id"])
    assert approval.status == "pending" and approval.scope.allowed_tools == []
    assert state["approval"]["action"]["execution"] == {"mode": "enforced", "guarded": True}
    assert state["approval"]["action"]["world_version"] == R


def test_reality_diff_shows_the_divergence_and_the_violation(tmp_path):
    _, state = start(tmp_path)
    diff = only_experiment(state)["diff"]
    assert diff["world_version"] == R and diff["violated_invariants"] == [INVARIANT]
    assert diff["first_divergence"]["event_id"] == 4
    assert diff["baseline_metrics"]["mutations_after_takeover"] == 0
    assert diff["faulted_metrics"]["mutations_after_takeover"] == 1


def test_baseline_and_fork_run_under_the_same_enforcement(tmp_path):
    lab, state = start(tmp_path)
    experiment = only_experiment(state)
    baseline, fork = load(lab, experiment["baseline"]), load(lab, experiment["fork"])
    assert baseline["adapter_id"] == fork["adapter_id"] == LOCAL
    for run in (baseline, fork):  # no guard denial in either: the guard was off for both
        assert not [e for e in run["trace"] if "error" in e["outcome"]]
    assert baseline["checks"][INVARIANT] and not fork["checks"][INVARIANT]


@pytest.mark.parametrize("faults, evidence", [
    ([COMPETITION], {"allocations_within_capacity": [2, 6],
                     "confirmations_backed_by_reservations": [4, 7]}),
    ([LOST_ACK], {"allocations_within_capacity": [3]}),
])
def test_every_fault_is_detected_through_the_controller(tmp_path, faults, evidence):
    _, state = start(tmp_path, relay_reply(faults=faults))
    assert {f["invariant"]: f["evidence_event_ids"] for f in failed(state)} == evidence


def test_findings_reach_memory_as_verified_relay_records(tmp_path):
    lab, state = start(tmp_path)
    scope = supervisor_scope(PROJECT, OPERATOR)
    (record,) = lab.memory_store.query(scope, memory_query())
    assert record.world_version == R and record.status == "verified"
    assert f"{INVARIANT} failed" in record.content and "mutations_after_takeover" in record.content
    assert record.record_id in state["memory_ids"]
    assert lab.memory_store.query(scope, memory_query(world="group-journey/1")) == []


def test_the_explorer_gets_the_relay_skill_and_schema(tmp_path):
    lab, _ = start(tmp_path)
    (request,) = lab.script.requests
    body = request.content.decode()
    assert "synthetic small business relay world" in body
    assert "world_experiment_proposal" in body and "takeover_with_queued_confirmation" in body


def test_a_later_relay_campaign_sees_relay_memory_only(tmp_path):
    lab, _ = start(tmp_path)
    lab.script.add(relay_reply())
    with lab.controller() as controller:
        controller.start("campaign-2", relay_spec())
    assert f"{INVARIANT} failed" in lab.script.requests[1].content.decode()


def test_the_campaign_record_is_the_world_contract(tmp_path):
    lab, _ = start(tmp_path)
    with lab.controller() as controller:
        record = controller.record(CID)
    assert type(record) is WorldCampaignRecord
    assert record.spec.world_version == R and record.status == "awaiting_approval"


# --- 2. the approved repair runs on the guarded boundary ---------------------------------

def test_approved_repair_runs_guarded_and_records_its_configuration(tmp_path):
    lab, state = start(tmp_path)
    done = resume_after_approval(lab, state)
    assert (done["status"], done["outcome"]) == ("completed", "repair_verified")
    repair = done["repair"]
    assert repair["execution"] == {"mode": "enforced", "guarded": True}
    assert repair["adapter"] == CHECKED and repair["verified"]
    assert repair["held_by"] == "adapter" and repair["guard_denials"] == []  # the adapter held
    assert repair["task_complete"] is False  # the owner has job-1, which is a fine outcome
    assert repair["task_outcome"]["jobs"] == {"job-1": "owner_takeover", "job-2": "confirmed"}
    run = load(lab, repair["run"])
    assert run["adapter_id"] == CHECKED and all(run["checks"].values())
    assert lab.approval_store.get(PROJECT, state["approval"]["approval_id"]).status == "consumed"


def test_the_takeover_race_is_caught_in_the_lab_and_closed_only_by_the_boundary(tmp_path):
    lab, state = start(tmp_path, relay_reply(CHECKED, faults=[TAKEOVER_RACE]))
    (finding,) = failed(state)
    assert finding["evidence_event_ids"] == [FIRST_CONFIRM_EVENT]
    lab_fork = load(lab, only_experiment(state)["fork"])
    assert lab_fork["final_state"]["ownership_timeline"][-1]["from_event"] == FIRST_CONFIRM_EVENT

    done = resume_after_approval(lab, state)
    repair = done["repair"]
    assert done["outcome"] == "repair_verified" and repair["held_by"] == "guarded_boundary"
    assert repair["guard_denials"] == [{"event_id": FIRST_CONFIRM_EVENT,
                                        "error": "OwnerTakeoverDenied", "job_id": "job-1"}]
    run = load(lab, repair["run"])
    assert all(run["checks"].values())
    assert [row["job_id"] for row in run["final_state"]["confirmation_log"]] == ["job-2"]


def test_the_capacity_race_is_closed_by_the_boundary(tmp_path):
    lab, state = start(tmp_path, relay_reply(CHECKED, faults=[CAPACITY_RACE]))
    assert failed(state)[0]["evidence_event_ids"] == [FIRST_LEASE_EVENT]
    done = resume_after_approval(lab, state)
    repair = done["repair"]
    assert done["outcome"] == "repair_verified" and repair["held_by"] == "guarded_boundary"
    assert [d["error"] for d in repair["guard_denials"]] == ["CapacityDenied"]
    assert repair["task_outcome"]["jobs"] == {"job-1": "capacity_lost", "job-2": "confirmed"}


def test_a_lost_acknowledgement_is_not_a_guard_denial(tmp_path):
    lab, state = start(tmp_path, relay_reply(faults=[LOST_ACK]))
    done = resume_after_approval(lab, state)
    repair = done["repair"]
    assert done["outcome"] == "repair_verified" and repair["task_complete"] is True
    assert repair["held_by"] == "adapter" and repair["guard_denials"] == []
    run = load(lab, repair["run"])
    assert run["trace"][1]["outcome"]["error"] == "ReservationAckLost"


def test_competition_leaves_the_job_it_cannot_keep_unconfirmed(tmp_path):
    lab, state = start(tmp_path, relay_reply(faults=[COMPETITION]))
    done = resume_after_approval(lab, state)
    repair = done["repair"]
    assert done["outcome"] == "repair_verified" and repair["task_complete"] is False
    assert repair["task_outcome"]["jobs"] == {"job-1": "capacity_lost", "job-2": "confirmed"}


def binding(actor=AUTOMATION):
    return StaticWorldBinding({(PROJECT, OPERATOR.principal_id, R): actor})


def run_directly(adapter, faults, mode, *, actor=AUTOMATION, project=PROJECT, fixture=None):
    return execute_relay_run(fixture or RelayFixture(), RelayScenario.from_json(
        scenario_json(faults, 20)), adapter, mode, scope=DRIVER.target(PROJECT, OPERATOR),
        project_id=project, binding=binding(actor))


RACE = [("takeover_with_queued_confirmation", FIRST_CONFIRM_EVENT - 1, {})]


def test_a_guarded_run_denies_a_confirmation_at_the_takeover_event():
    outcome = run_directly(CHECKED, RACE, ENFORCED)
    assert outcome.denial is None and all(outcome.run["checks"].values())
    denial = outcome.run["trace"][FIRST_CONFIRM_EVENT - 1]
    assert denial["request"]["tool"] == "confirm_service"
    assert denial["outcome"]["error"] == "OwnerTakeoverDenied"


def test_the_same_run_without_the_guard_confirms():
    outcome = run_directly(CHECKED, RACE, LAB)
    assert not outcome.run["checks"][INVARIANT]
    assert outcome.run["trace"][FIRST_CONFIRM_EVENT - 1]["outcome"]["result"]["status"] == "confirmed"


def test_a_confirmation_before_the_takeover_stays_permitted_when_guarded():
    outcome = run_directly(CHECKED, [("takeover_with_queued_confirmation", 5, {})], ENFORCED)
    assert all(outcome.run["checks"].values())
    assert not [e for e in outcome.run["trace"] if "error" in e["outcome"]]


def test_lab_runs_open_no_connection(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a world run opened a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert run_directly(LOCAL, RACE, LAB).run


# --- 3. the target's tools ----------------------------------------------------------------

def test_the_target_holds_every_tool_of_the_pack():
    assert set(DRIVER.target(PROJECT, OPERATOR).allowed_tools) == {
        "inspect_availability", "reserve_resource", "release_resource", "confirm_service",
        "hand_off_to_owner"}


def test_a_repair_hands_off_through_the_authorized_tool(tmp_path):
    lab, state = start(tmp_path, relay_reply(faults=[COMPETITION]))
    done = resume_after_approval(lab, state)
    assert done["repair"]["task_outcome"]["handed_off"] == ["job-1"]
    run = load(lab, done["repair"]["run"])
    assert run["trace"][1]["request"]["tool"] == "hand_off_to_owner"
    assert run["trace"][1]["outcome"]["result"]["owner"] == "business_owner"


def test_only_the_automation_can_hand_off():
    # The business owner's identity may read, but M06 refuses it the first change.
    outcome = run_directly(CHECKED, [("stale_availability_competition", None,
                                      {"competing_jobs": 2})], LAB, actor=BUSINESS_OWNER)
    assert outcome.run is None and outcome.denial.code == "identity_mismatch"


def test_a_policy_denial_is_not_something_an_adapter_can_catch():
    assert not issubclass(RelayToolDenied, ToolError)


# --- 4. capsule export and replay make no model calls ------------------------------------

@pytest.mark.parametrize("run", ["baseline", "fork", "repaired"])
def test_capsules_replay_with_zero_model_calls(tmp_path, run):
    lab, state = start(tmp_path)
    resume_after_approval(lab, state)
    calls = len(lab.script.requests)
    with lab.controller() as controller:
        ref = controller.export_capsule(CID, run=run)
        again = controller.export_capsule(CID, run=run)
        replayed = controller.replay_capsule(CID, run=run)
    assert ref == again  # idempotent
    assert replayed["reproduced"] and replayed["model_calls"] == 0
    assert replayed["guarded"] is (run == "repaired")
    assert len(lab.script.requests) == calls
    payload = CapsuleV2.model_validate(load(lab, ref)).payload
    assert payload.scenario.world_version == R


def test_a_baseline_capsule_records_a_fault_free_relay_scenario(tmp_path):
    lab, _ = start(tmp_path)
    with lab.controller() as controller:
        ref = controller.export_capsule(CID, run="baseline")
    scenario = load(lab, ref)["payload"]["scenario"]
    assert scenario == scenario_json(max_tool_calls=20)


def test_the_race_capsule_reproduces_the_guard_denial(tmp_path):
    lab, state = start(tmp_path, relay_reply(CHECKED, faults=[TAKEOVER_RACE]))
    resume_after_approval(lab, state)
    with lab.controller() as controller:
        ref = controller.export_capsule(CID, run="repaired")
        replayed = controller.replay_capsule(CID, run="repaired")
    assert replayed["reproduced"] and replayed["guarded"] and replayed["model_calls"] == 0
    assert all(replayed["checks"].values())
    payload = load(lab, ref)["payload"]
    assert payload["fixture"]["guarded"] is True
    assert payload["recorded_model_outputs"] == []


# --- 5. restart and recovery -------------------------------------------------------------

class CrashOnce:
    def __init__(self, node):
        self.node, self.fired = node, False

    def __call__(self, name, state):
        if name == self.node and not self.fired:
            self.fired = True
            raise RuntimeError(f"crash before {name}")


@pytest.mark.parametrize("node", ["execute", "verify", "remember", "request_repair"])
def test_recovery_continues_without_repeating_model_or_world_work(tmp_path, node):
    lab = RelayLab(tmp_path, Script(relay_reply()), on_node=CrashOnce(node))
    with lab.controller() as controller, pytest.raises(RuntimeError):
        controller.start(CID, relay_spec())
    with lab.controller() as controller:
        state = controller.recover(CID)
    assert state["paused"] and len(lab.script.requests) == 1
    assert len(state["experiments"]) == 1 and len(failed(state)) == 1
    assert open_reservations(lab) == []


def test_a_paused_campaign_survives_a_restart(tmp_path):
    lab, state = start(tmp_path)
    fresh = RelayLab(tmp_path, Script())
    fresh.clock.now = lab.clock.now
    fresh.approve(state)
    with fresh.controller() as controller:
        done = controller.resume(CID)
    assert done["outcome"] == "repair_verified"
    assert len(fresh.script.requests) == 0


# --- 6. project isolation ----------------------------------------------------------------

OTHER = TrustedSession(STRANGER, "project-b")


@pytest.mark.parametrize("call", [
    lambda c: c.state(CID), lambda c: c.resume(CID), lambda c: c.recover(CID),
    lambda c: c.record(CID), lambda c: c.export_capsule(CID), lambda c: c.replay_capsule(CID)])
def test_another_project_reaches_nothing(tmp_path, call):
    lab, _ = start(tmp_path)
    with lab.controller() as controller:
        controller.export_capsule(CID)
    with lab.controller(OTHER) as controller, pytest.raises(PolicyDenied) as denied:
        call(controller)
    assert denied.value.decision.code == "cross_project"


def test_a_run_artifact_is_invisible_to_another_project(tmp_path):
    lab, state = start(tmp_path)
    fork = only_experiment(state)["fork"]
    other = lab.artifacts.for_project(supervisor_scope("project-b", STRANGER))
    with pytest.raises(NotFound):
        other.ref(fork["id"])


# --- 7. denied actions -------------------------------------------------------------------

def test_a_principal_bound_to_the_business_owner_cannot_drive_the_automation(tmp_path):
    lab, state = start(tmp_path, world_binding=binding(BUSINESS_OWNER))
    assert (state["status"], state["outcome"]) == ("failed", "policy_denied")
    assert "identity_mismatch" in state["errors"][-1]["message"]
    assert state["errors"][-1]["side_effect"] == "none"
    assert state["experiments"] == {} and state["findings"] == []
    assert open_reservations(lab) == []


def test_an_unbound_principal_runs_nothing(tmp_path):
    lab, state = start(tmp_path, world_binding=StaticWorldBinding({}))
    assert (state["status"], state["outcome"]) == ("failed", "policy_denied")
    assert "unbound_principal" in state["errors"][-1]["message"]
    assert open_reservations(lab) == []


def test_an_unbound_principal_is_refused_before_a_world_exists():
    with pytest.raises(PolicyDenied) as refused:
        execute_relay_run(RelayFixture(), RelayScenario.from_json(scenario_json()), LOCAL, LAB,
                          scope=DRIVER.target(PROJECT, OPERATOR), project_id=PROJECT,
                          binding=StaticWorldBinding({}))
    assert refused.value.decision.code == "unbound_principal"


def test_ownership_holds_in_the_lab_too():
    outcome = run_directly(LOCAL, [], LAB, actor=BUSINESS_OWNER)
    assert outcome.run is None and outcome.denial.code == "identity_mismatch"
    assert outcome.tool_calls == 1  # the read passed; the first change was refused


def test_a_scope_for_another_project_denies_every_call():
    outcome = execute_relay_run(
        RelayFixture(), RelayScenario.from_json(scenario_json(max_tool_calls=20)), LOCAL, LAB,
        scope=DRIVER.target(PROJECT, OPERATOR), project_id="project-b", binding=StaticWorldBinding(
            {("project-b", OPERATOR.principal_id, R): AUTOMATION}))
    assert outcome.denial.code == "cross_project" and outcome.tool_calls == 0


def test_a_rejected_repair_runs_nothing(tmp_path):
    lab, state = start(tmp_path)
    lab.approve(state, decision="reject")
    with lab.controller() as controller:
        done = controller.resume(CID)
    assert done["outcome"] == "repair_rejected" and not (done["repair"] or {}).get("run")


def test_resuming_without_a_decision_grants_nothing(tmp_path):
    lab, state = start(tmp_path)
    with lab.controller() as controller:
        again = controller.resume(CID)
    assert again["paused"] and again["repair"] is None


def test_a_digest_for_another_action_cannot_decide(tmp_path):
    lab, state = start(tmp_path)
    with pytest.raises(ApprovalDenied):
        lab.approve(state, digest="0" * 64)


# --- 8. nothing untrusted reaches enforcement ---------------------------------------------

@pytest.mark.parametrize("field, value", [("guarded", False), ("execution_mode", "enforced"),
                                          ("actor_id", AUTOMATION), ("approved", True)])
def test_a_proposal_carrying_enforcement_or_authority_never_runs(tmp_path, field, value):
    bad = meta_reply(json.dumps(proposal_json(**{field: value})))
    lab, state = start(tmp_path, bad, bad)
    assert state["status"] == "failed" and state["experiments"] == {}


def test_the_repair_mode_comes_from_policy_not_the_action():
    action = DRIVER.repair_action("exp-1", proposal_json())
    assert DRIVER.repair_request(action).mode == ENFORCED
    with pytest.raises(PolicyDenied):
        DRIVER.repair_request({**action, "execution": LAB.as_json()})


def test_exporting_a_lab_capsule_does_not_weaken_the_repair(tmp_path):
    lab, state = start(tmp_path)
    with lab.controller() as controller:
        controller.export_capsule(CID, run="fork")
        assert controller.replay_capsule(CID, run="fork")["guarded"] is False
    done = resume_after_approval(lab, state)
    assert done["repair"]["execution"] == {"mode": "enforced", "guarded": True}


# --- 9. budgets, deadlines and cancellation -----------------------------------------------

def test_a_proposal_over_the_target_tool_limit_is_rejected(tmp_path):
    lab, state = start(tmp_path, relay_reply(cap=21), relay_reply(cap=21))
    assert state["experiments"] == {} and state["outcome"] == "no_failure_found"
    assert any("exceeds the target role ceiling" in e["message"] for e in state["errors"])


def test_the_limit_itself_is_accepted(tmp_path):
    _, state = start(tmp_path, relay_reply(cap=20))
    assert len(state["experiments"]) == 1


def test_a_campaign_budget_that_cannot_cover_the_repair_stops_it(tmp_path):
    # The experiment spends 14 of 25, leaving 11: one short of the repair's cap of 12.
    lab = RelayLab(tmp_path, Script(relay_reply(cap=12)))
    with lab.controller() as controller:
        state = controller.start(CID, relay_spec(max_tool_calls=25))
    lab.approve(state)
    with lab.controller() as controller:
        done = controller.resume(CID)
    assert (done["status"], done["outcome"]) == ("budget_exhausted", "tool_calls")
    assert not done["repair"].get("run") and open_reservations(lab) == []


class TripAfter:
    """A trusted binding that fires a callback once ``count`` tool calls were authorized."""

    def __init__(self, count, action):
        self.count, self.action, self.seen = count, action, 0

    def actor_for(self, project_id, principal, world_version):
        self.seen += 1
        if self.seen == self.count:
            self.action()
        return AUTOMATION


def test_cancellation_stops_a_run_before_its_next_tool_call(tmp_path):
    cancel = threading.Event()
    lab = RelayLab(tmp_path, Script(relay_reply()), world_binding=TripAfter(6, cancel.set))
    with lab.controller(cancel=cancel) as controller:
        state = controller.start(CID, relay_spec(max_parallel_runs=1))
    assert (state["status"], state["outcome"]) == ("cancelled", "cancelled_by_request")
    assert state["experiments"] == {} and state["findings"] == []
    assert open_reservations(lab) == []


def test_the_deadline_stops_a_run_before_its_next_tool_call(tmp_path):
    lab = RelayLab(tmp_path, Script(relay_reply()))
    lab.world_binding = TripAfter(6, lambda: lab.clock.advance(minutes=21))
    with lab.controller() as controller:
        state = controller.start(CID, relay_spec(max_parallel_runs=1))
    assert (state["status"], state["outcome"]) == ("budget_exhausted", "wall_clock_ms")
    assert state["experiments"] == {} and open_reservations(lab) == []


# --- 10. only the world this campaign is for ------------------------------------------------

def test_a_proposal_for_another_world_is_rejected(tmp_path):
    other = journey_reply()
    _, state = start(tmp_path, other, other)
    assert state["experiments"] == {}
    assert any("not this campaign's world" in e["message"] for e in state["errors"])


@pytest.mark.parametrize("entry", ["resume", "recover"])
def test_the_gate_is_rechecked_on_resume_and_recovery(tmp_path, entry):
    lab, _ = start(tmp_path)
    lab.worlds = {}
    with lab.controller() as controller, pytest.raises(PolicyDenied) as denied:
        getattr(controller, entry)(CID)
    assert denied.value.decision.code == "world_not_runnable"


def test_the_gate_is_rechecked_before_execution(tmp_path, monkeypatch):
    def withdraw(name, state):
        if name == "execute":
            monkeypatch.setattr("ghostlab.policy.world.is_executable", lambda world: False)
    lab = RelayLab(tmp_path, Script(relay_reply()), on_node=withdraw)
    with lab.controller() as controller:
        state = controller.start(CID, relay_spec())
    assert (state["status"], state["outcome"]) == ("failed", "world_not_runnable")
    assert state["experiments"] == {} and open_reservations(lab) == []


def test_every_implemented_world_has_a_driver():
    assert set(DEFAULT_WORLDS) == {"refund-world/1", "group-journey/1", R}


# --- 11. the campaign's fixture is pinned ---------------------------------------------------

CHANGED = RelayFixture(jobs=(RelayJob("job-9", "conversation-9", None),))


def test_the_fixture_is_recorded_when_the_campaign_starts(tmp_path):
    _, state = start(tmp_path)
    assert [job["job_id"] for job in state["fixture"]["jobs"]] == ["job-1", "job-2"]
    assert len(state["fixture_sha256"]) == 64
    assert state["approval"]["action"]["fixture_sha256"] == state["fixture_sha256"]


def test_a_changed_fixture_cannot_use_an_existing_approval(tmp_path):
    lab, state = start(tmp_path)
    lab.approve(state)
    lab.relay_fixture = CHANGED  # a restart with a different world
    with lab.controller() as controller:
        with pytest.raises(CampaignSettingsChanged, match="fixture changed"):
            controller.resume(CID)
        assert controller.state(CID)["repair"] is None
    approval = lab.approval_store.get(PROJECT, state["approval"]["approval_id"])
    assert approval.status == "approved"  # not consumed, so nothing was spent
    lab.relay_fixture = None  # the original settings still work
    with lab.controller() as controller:
        assert controller.resume(CID)["outcome"] == "repair_verified"


def test_consumption_rechecks_the_fixture(tmp_path):
    lab, state = start(tmp_path)
    lab.approve(state)
    held = {}

    def swap(name, _):
        if name == "consume":
            held["controller"].s.relay_fixture = CHANGED

    lab.on_node = swap
    with lab.controller() as controller:
        held["controller"] = controller
        done = controller.resume(CID)
    assert (done["status"], done["outcome"]) == ("failed", "settings_changed")
    assert lab.approval_store.get(PROJECT, state["approval"]["approval_id"]).status == "approved"


def test_the_run_and_its_verification_use_the_pinned_fixture(tmp_path):
    lab = RelayLab(tmp_path, Script(relay_reply()))
    held = {}

    def swap(name, _):
        if name == "execute":
            held["controller"].s.relay_fixture = CHANGED

    lab.on_node = swap
    with lab.controller() as controller:
        held["controller"] = controller
        state = controller.start(CID, relay_spec())
    assert failed(state)[0]["evidence_event_ids"] == [4]


@pytest.mark.parametrize("run", ["baseline", "fork", "repaired"])
def test_an_export_after_a_fixture_change_still_reproduces(tmp_path, run):
    lab, state = start(tmp_path)
    resume_after_approval(lab, state)
    lab.relay_fixture = CHANGED
    with lab.controller() as controller:
        ref = controller.export_capsule(CID, run=run)
        replayed = controller.replay_capsule(CID, run=run)
    assert replayed["reproduced"] and replayed["model_calls"] == 0
    assert load(lab, ref)["payload"]["fixture"]["jobs"][0]["job_id"] == "job-1"


def test_the_guard_flag_is_not_part_of_the_pinned_world(tmp_path):
    lab, state = start(tmp_path, relay_fixture=RelayFixture(guarded=True))
    assert state["fixture"]["guarded"] is False
    assert only_experiment(state)["execution"] == LAB.as_json()


def test_a_tampered_snapshot_is_refused(tmp_path):
    lab, state = start(tmp_path)
    with lab.controller() as controller:
        config = controller._config(CID)
        controller.graph.update_state(config, {
            "fixture": {**state["fixture"], "operator": "someone-else"}})
        with pytest.raises(CampaignSettingsChanged, match="digest"):
            controller.resume(CID)


def test_a_relay_campaign_without_a_recorded_fixture_is_refused(tmp_path):
    lab, state = start(tmp_path)
    with lab.controller() as controller:
        controller.graph.update_state(controller._config(CID),
                                      {"fixture": None, "fixture_sha256": None})
        with pytest.raises(CampaignSettingsChanged, match="no recorded fixture"):
            controller.resume(CID)


# --- 12. a verified repair has to have run ---------------------------------------------------

def test_a_truncated_repair_is_incomplete_not_verified(tmp_path):
    lab, state = start(tmp_path, relay_reply(cap=7))  # enough to detect, too few to repair
    assert failed(state)[0]["evidence_event_ids"] == [4]
    done = resume_after_approval(lab, state)
    assert (done["status"], done["outcome"]) == ("budget_exhausted", "repair_incomplete")
    repair = done["repair"]
    assert repair["verified"] is False and repair["run_status"] == "budget_exhausted"
    assert repair["safety_held"] is True and repair["held_by"] is None  # no violation, no proof
    run = load(lab, repair["run"])
    assert run["status"] == "budget_exhausted" and len(run["trace"]) == 7


def test_a_repair_with_exactly_enough_calls_is_verified(tmp_path):
    lab, state = start(tmp_path, relay_reply(cap=8))
    done = resume_after_approval(lab, state)
    assert done["outcome"] == "repair_verified" and done["repair"]["run_status"] == "completed"


def test_a_guard_denial_stays_successful_enforcement(tmp_path):
    lab, state = start(tmp_path, relay_reply(CHECKED, faults=[TAKEOVER_RACE], cap=10))
    done = resume_after_approval(lab, state)
    assert done["outcome"] == "repair_verified"
    assert done["repair"]["held_by"] == "guarded_boundary"


def test_a_repair_that_does_nothing_verifies_nothing(tmp_path):
    # Every lease lapses by the next event, so the repaired adapter never confirms anything.
    lab, state = start(tmp_path, relay_reply(faults=[]),
                       relay_fixture=RelayFixture(lease_ttl_events=1))
    assert failed(state)
    done = resume_after_approval(lab, state)
    assert (done["status"], done["outcome"]) == ("failed", "repair_incomplete")
    repair = done["repair"]
    assert repair["safety_held"] and repair["run_status"] == "completed"
    assert repair["verified"] is False and repair["held_by"] is None
    # it handed every job off, which is safe and proves nothing
    assert set(repair["task_outcome"]["jobs"].values()) == {"unresolved"}
    assert repair["task_outcome"]["handed_off"] == ["job-1", "job-2"]


PASSED = [{"verdict": "passed"}]
GUARDED = {"mode": "enforced", "guarded": True}


def repaired_run(faults):
    return run_directly(CHECKED, faults, ENFORCED).run


def assess(run):
    return DRIVER.assess_repair(RelayFixture(), run, PASSED, GUARDED)


def without_confirmation(run, job_id):
    state = run["final_state"]
    state["confirmation_log"] = [row for row in state["confirmation_log"]
                                 if row["job_id"] != job_id]
    return run


def test_a_clean_finish_is_verified_and_a_missing_job_is_not():
    run = repaired_run([])
    assert assess(run)["verified"]
    assert assess(without_confirmation(run, "job-2"))["verified"] is False


def test_a_finished_repair_that_broke_an_invariant_is_not_verified():
    run = repaired_run([])
    assessment = DRIVER.assess_repair(RelayFixture(), run, [{"verdict": "failed"}], GUARDED)
    assert assessment["safety_held"] is False and assessment["verified"] is False


def test_a_denial_for_one_job_does_not_excuse_another():
    run = repaired_run(RACE)
    assert assess(run)["verified"]  # job-1 was refused; job-2 was confirmed
    assert assess(without_confirmation(run, "job-2"))["verified"] is False


def test_a_denial_explains_the_job_it_refused():
    run = repaired_run([])
    without_confirmation(run, "job-2")
    denied = next(event for event in run["trace"] if event["request"]["tool"] == "confirm_service"
                  and event["request"]["arguments"]["job_id"] == "job-2")
    denied["outcome"] = {"error": "StalePromiseDenied", "message": "stale"}
    assert assess(run)["verified"]


def test_a_lost_acknowledgement_explains_nothing():
    run = repaired_run([])
    without_confirmation(run, "job-2")
    reserved = next(event for event in run["trace"] if event["request"]["tool"] == "reserve_resource"
                    and event["request"]["arguments"]["job_id"] == "job-2")
    reserved["outcome"] = {"error": "ReservationAckLost", "message": "lost"}
    assert assess(run)["verified"] is False


def test_a_denial_in_a_truncated_run_is_not_credited_to_the_boundary():
    run = repaired_run(RACE)
    run["status"] = "budget_exhausted"
    assessment = assess(run)
    assert assessment["verified"] is False
    result = DRIVER.repair_result(run, {"adapter": CHECKED, "execution": GUARDED},
                                  assessment["verified"])
    assert result["held_by"] is None and result["guard_denials"]


# --- 13. model-controlled text is redacted before it is stored ------------------------------

SECRET = "sk-synthetic0123456789abcdef"
BEARER = "Bearer synthetic-bearer-value-0123"
LEAKY = {"hypothesis": f"Replay with {SECRET} to reach the confirmation",
         "expected_signal": f"{INVARIANT} fails; use {BEARER}"}


def stored_bytes(root):
    """Every byte the campaign left on disk: databases, WAL files and artifacts."""
    return b"".join(path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file())


def test_a_secret_in_a_proposal_never_reaches_state_or_storage(tmp_path):
    lab, state = start(tmp_path, meta_reply(json.dumps(proposal_json(**LEAKY))))
    done = resume_after_approval(lab, state)
    with lab.controller() as controller:
        record = controller.record(CID).model_dump_json()
        controller.export_capsule(CID, run="fork")
        controller.export_capsule(CID, run="repaired")
    for text in (json.dumps(state), json.dumps(done), record):
        assert SECRET not in text and BEARER not in text
    assert "[REDACTED]" in only_experiment(done)["proposal"]["hypothesis"]
    data = stored_bytes(tmp_path)
    assert SECRET.encode() not in data and BEARER.encode() not in data


def test_benign_proposals_are_unchanged(tmp_path):
    _, state = start(tmp_path)
    assert only_experiment(state)["proposal"] == proposal_json()
    assert not any("redacted" in e["summary"] for e in state["events"])


# --- 14. a truncated experiment is inconclusive ---------------------------------------------

def test_a_truncated_experiment_reports_incomplete_coverage(tmp_path):
    lab, state = start(tmp_path, relay_reply(cap=3), relay_reply(cap=2))
    assert (state["status"], state["outcome"]) == ("budget_exhausted", "coverage_incomplete")
    assert not failed(state)
    for experiment in state["experiments"].values():
        assert experiment["coverage"] == {"baseline": "budget_exhausted",
                                          "fork": "budget_exhausted", "complete": False}
    with lab.controller() as controller:
        assert controller.record(CID).status == "budget_exhausted"


def test_a_violation_seen_before_truncation_is_retained(tmp_path):
    lab, state = start(tmp_path, relay_reply(cap=4))  # the confirmation is the last call
    (finding,) = failed(state)
    assert (finding["invariant"], finding["evidence_event_ids"]) == (INVARIANT, [4])
    assert only_experiment(state)["coverage"]["complete"] is False
    assert state["paused"] and state["status"] == "awaiting_approval"  # still goes to a human


def test_a_finished_experiment_with_no_failure_is_still_no_failure_found(tmp_path):
    _, state = start(tmp_path, relay_reply(CHECKED, faults=[]),
                     relay_reply(CHECKED, faults=[], cap=19))
    assert (state["status"], state["outcome"]) == ("completed", "no_failure_found")


# --- 15. three worlds side by side ----------------------------------------------------------

EVERY_WORLD = StaticWorldBinding({(PROJECT, OPERATOR.principal_id, J): "traveller-a",
                                  (PROJECT, OPERATOR.principal_id, R): AUTOMATION})


def test_the_three_worlds_stay_distinguishable(tmp_path):
    lab = RelayLab(tmp_path, Script(proposal(), journey_reply(), relay_reply()),
                   world_binding=EVERY_WORLD)
    with lab.controller() as controller:
        refund = controller.start("refund-1", spec())
        journey = controller.start("journey-1", journey_spec())
        relay = controller.start("relay-1", relay_spec())
    assert "execution" not in only_experiment(refund)
    assert only_experiment(journey)["execution"] == LAB.as_json()
    assert only_experiment(relay)["execution"] == LAB.as_json()
    assert lab.engine.calls == [(False, "vulnerable"), (True, "vulnerable")]  # refund engine only
    scope = supervisor_scope(PROJECT, OPERATOR)
    worlds = {world: lab.memory_store.query(scope, memory_query(world=world))
              for world in ("refund-world/1", "group-journey/1", R)}
    assert {w: {r.world_version for r in records} for w, records in worlds.items()} == {
        w: {w} for w in worlds}


def test_the_relay_explorer_never_sees_another_worlds_memory(tmp_path):
    lab = RelayLab(tmp_path, Script(proposal(), journey_reply(), relay_reply()),
                   world_binding=EVERY_WORLD)
    with lab.controller() as controller:
        controller.start("refund-1", spec())
        controller.start("journey-1", journey_spec())
        controller.start("relay-1", relay_spec())
    body = lab.script.requests[2].content.decode()
    assert "total_refund_within_payment" not in body and "delivered_messages_permitted" not in body


def test_the_journey_fixture_setting_does_not_reach_a_relay_campaign(tmp_path):
    _, state = start(tmp_path)
    assert "journey_id" not in state["fixture"] and state["fixture"]["shop_id"] == "shop-1"
    assert replace(RelayFixture.from_json(state["fixture"]), guarded=False) == RelayFixture()


def test_every_tool_call_of_a_run_is_reconciled(tmp_path):
    lab, state = start(tmp_path)
    with lab.controller() as controller:
        usage = controller.record(CID).usage
    assert usage.tool_calls == 14  # 7 calls in each of the baseline and the fork
    assert open_reservations(lab) == []
