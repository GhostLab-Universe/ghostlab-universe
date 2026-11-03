"""P1 migration proof: the refund world behind the frozen extension interfaces.

The direct ``ghostlab.core`` path is untouched, so it is the oracle: every
test here compares new-path answers (engine, driver, binding, generic
helpers) against legacy functions and proves them identical. A stub world
then runs end to end through the generic driver with no driver or controller
change, proving new environments need no domain-specific conditions.
"""

import inspect
import json
from dataclasses import asdict
from types import SimpleNamespace

from ghostlab import capsule as refund_capsule
from ghostlab.adapters import default_registry
from ghostlab.adapters.refund_env import RefundEnvironment
from ghostlab.contracts.world_packs import pack
from ghostlab.core import Fixture, Scenario, reality_diff, run as core_run
from ghostlab.extensions.runner import first_divergence, replay_trace, run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.orchestration import (DEFAULT_WORLDS, ExtensionBinding, ExtensionDriver,
                                    derive_findings, execute_run, execute_with_runner)
from ghostlab.orchestration.engine import ledger_change_events
from ghostlab.orchestration.worlds import (REFUND_BINDING, REFUND_WORLD, RefundDriver,
                                           RunRequest, baseline_scenario)
from ghostlab.contracts import Principal
from ghostlab.policy import StaticPrincipalBinding, delegate, root_scope

from m05_harness import OPERATOR, PROJECT

RECORDED_AT = "2026-10-29T12:00:00Z"
BINDING = StaticPrincipalBinding({(PROJECT, OPERATOR.principal_id): "synthetic-customer-1"})
TARGET = delegate(root_scope(PROJECT, OPERATOR), "target",
                  {"get_order", "issue_refund", "get_refund_status"})
PROPOSAL = {"adapter": "vulnerable",
            "scenario": {"timeout_after_commit": True, "max_tool_calls": 20}}


def driver():
    return DEFAULT_WORLDS[REFUND_WORLD]


def test_refund_driver_is_the_generic_driver_with_the_refund_binding():
    assert isinstance(driver(), RefundDriver)
    assert isinstance(driver(), ExtensionDriver)
    assert driver().binding is REFUND_BINDING
    assert RefundDriver().binding is REFUND_BINDING
    assert ExtensionBinding.__dataclass_params__.frozen
    assert RefundDriver.world_version == REFUND_WORLD == driver().world_version


def test_experiment_runs_match_the_legacy_pair():
    runs = driver().experiment_runs(PROPOSAL)
    assert [(request.scenario.timeout_after_commit, request.scenario.max_tool_calls,
             request.adapter, request.cap, request.mode) for request in runs] == [
        (False, 20, "vulnerable", 20, None), (True, 20, "vulnerable", 20, None)]
    assert driver().cap(PROPOSAL) == 20
    assert driver().problems(PROPOSAL) == []
    assert driver().explored(PROPOSAL) == "adapter=vulnerable timeout_after_commit=True"


def test_execute_request_keeps_the_engine_seam():
    calls = []

    def spy(fixture, scenario, adapter, **kwargs):
        calls.append((scenario.timeout_after_commit, adapter, kwargs))
        return "outcome"

    services = SimpleNamespace(engine=spy, binding=BINDING)
    request = RunRequest(Scenario(timeout_after_commit=True, max_tool_calls=20),
                         "vulnerable", 20, None)
    assert driver().execute(services, Fixture(), request, scope=TARGET,
                            project_id=PROJECT, should_stop=lambda: None) == "outcome"
    assert len(calls) == 1
    timeout, adapter, kwargs = calls[0]
    assert (timeout, adapter) == (True, "vulnerable")
    assert kwargs["scope"] is TARGET and kwargs["project_id"] == PROJECT
    assert kwargs["binding"] is BINDING


def test_minimum_cap_matches_the_legacy_measurement():
    legacy = min(len(core_run(Fixture(), Scenario(timeout_after_commit=True), adapter)["trace"])
                 for adapter in ("vulnerable", "repaired"))
    assert legacy == 3
    assert driver().minimum_cap(Fixture()) == legacy


def test_faults_and_summaries_match():
    fork = core_run(Fixture(), Scenario(timeout_after_commit=True), "vulnerable")
    baseline = core_run(Fixture(), Scenario(), "vulnerable")
    assert driver().faults_fired(PROPOSAL, fork) == {"timeout_after_commit"}
    assert driver().faults_triggered(PROPOSAL, fork) is True
    assert driver().faults_triggered(PROPOSAL, baseline) is False
    driver().validate_run(fork)
    diff = driver().diff(Fixture(), baseline, fork)
    assert diff == driver().binding.assemble_diff(Fixture(), baseline, fork)
    assert driver().run_summary(diff).startswith("Baseline 250000 vs fork 500000")
    assert baseline_scenario(driver(), PROPOSAL) == {"timeout_after_commit": False,
                                                     "max_tool_calls": 20}


def test_repair_answers_match():
    assert driver().repair_candidate(PROPOSAL) is True
    assert driver().repair_candidate({**PROPOSAL, "adapter": "repaired"}) is False
    action = driver().repair_action("exp-1", PROPOSAL)
    assert action == {"kind": "apply_repair", "experiment_id": "exp-1",
                      "adapter": "repaired", "world_version": REFUND_WORLD,
                      "scenario": PROPOSAL["scenario"]}
    request = driver().repair_request(action)
    assert (request.scenario.timeout_after_commit, request.adapter) == (True, "repaired")
    repaired = core_run(Fixture(), Scenario(timeout_after_commit=True), "repaired")
    findings = derive_findings("exp-1-repaired", repaired, recorded_at=RECORDED_AT)
    assessment = driver().assess_repair(Fixture(), repaired, findings, None)
    assert assessment == {"run_status": "completed", "safety_held": True,
                          "task_complete": True, "verified": True}
    assert driver().repair_result(repaired, action, True) == {"refunded_minor": 250_000}
    assert "status check before retry" in driver().repair_summary


def test_capsule_bytes_and_replay_are_identical(tmp_path):
    fixture = Fixture()
    scenario = {"timeout_after_commit": True, "max_tool_calls": 20}
    run = core_run(fixture, Scenario(**scenario), "vulnerable")
    first, second = tmp_path / "driver.json", tmp_path / "legacy.json"
    driver().capsule(fixture, scenario, run, None, first)
    refund_capsule.export_capsule(second, fixture, Scenario(**scenario), run)
    assert first.read_bytes() == second.read_bytes()
    assert driver().replay(first) == refund_capsule.replay(second)


def test_generic_replay_agrees_with_capsule_replay(tmp_path):
    compared = 0
    for adapter in ("vulnerable", "repaired"):
        for timeout in (False, True):
            for cap in (20, 1):
                fixture = Fixture()
                scenario = {"timeout_after_commit": timeout, "max_tool_calls": cap}
                run = core_run(fixture, Scenario(**scenario), adapter)
                path = tmp_path / f"{adapter}-{timeout}-{cap}.json"
                refund_capsule.export_capsule(path, fixture, Scenario(**scenario), run)
                legacy = refund_capsule.replay(path)
                assert legacy["reproduced"] is True
                payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
                env = RefundEnvironment()
                env.reset(payload["fixture"], payload["scenario"],
                          actor_id=payload["actor_id"])
                replayed = replay_trace(env, payload["run"]["trace"])
                checks = {check.name: check.passed
                          for check in env.evaluate(trace=replayed.trace,
                                                    state=replayed.state)}
                assert list(replayed.trace) == payload["run"]["trace"]
                assert dict(replayed.state) == payload["run"]["final_state"]
                assert checks == payload["run"]["checks"] == legacy["checks"]
                assert replayed.state["total_refunded_minor"] == legacy[
                    "total_refunded_minor"]
                compared += 1
    assert compared == 8


def test_evaluate_matches_derive_findings():
    run = core_run(Fixture(), Scenario(timeout_after_commit=True), "vulnerable")
    legacy = derive_findings("exp-1-fork", run, recorded_at=RECORDED_AT)
    checks = RefundEnvironment().evaluate(trace=run["trace"], state=run["final_state"])
    assert [finding["invariant"] for finding in legacy] == [check.name for check in checks]
    for finding, check in zip(legacy, checks):
        assert (finding["verdict"] == "failed") == (not check.passed)
        assert finding["evidence_event_ids"] == list(check.evidence_event_ids)
        totals = check.name == "total_refund_within_payment"
        assert (finding["observed_minor"], finding["limit_minor"]) == (
            (check.observed, check.limit) if totals else (None, None))
    assert ledger_change_events(run) == [2, 3]


def test_describe_matches_the_reviewed_pack():
    described = {tool.name: (tool.effect, tool.parameters)
                 for tool in RefundEnvironment().describe().tools}
    declared = {spec.name: (spec.effect, spec.parameters)
                for spec in pack(REFUND_WORLD).tools}
    assert described == declared
    assert set(RefundEnvironment().describe().invariants) == pack(REFUND_WORLD).invariant_names


def test_first_divergence_matches_reality_diff():
    baseline = core_run(Fixture(), Scenario(), "vulnerable")
    faulted = core_run(Fixture(), Scenario(timeout_after_commit=True), "vulnerable")
    generic = first_divergence(baseline["trace"], faulted["trace"])
    legacy = reality_diff(baseline, faulted)["first_divergence"]
    assert generic["event_id"] == legacy["event_id"] == 2
    assert generic["baseline"] == legacy["baseline"]
    assert generic["faulted"] == legacy["faulted"]
    assert first_divergence(baseline["trace"], baseline["trace"]) is None


def test_generic_code_has_no_world_branches():
    source = inspect.getsource(ExtensionDriver) + inspect.getsource(execute_with_runner)
    assert "refund" not in source.lower()
    assert "journey" not in source.lower()


def test_identity_and_metadata_match():
    services = SimpleNamespace(binding=BINDING, fixture=Fixture())
    assert driver().snapshot(Fixture()) == asdict(Fixture())
    assert driver().restore(asdict(Fixture())) == Fixture()
    assert driver().current_fixture(services) == Fixture()
    assert driver().identity_problem(services, Fixture(), PROJECT, OPERATOR) is None
    stranger = Principal(principal_id="stranger-1", kind="human")
    assert driver().identity_problem(services, Fixture(), PROJECT, stranger) is not None
    assert driver().supervisor(PROJECT, OPERATOR).role == "supervisor"
    assert driver().target(PROJECT, OPERATOR).role == "target"
    assert driver().explorer_facts(Fixture()) == ()


# --- a new world through the generic driver, with no driver/controller change ---


class StubEnvironment:
    """Counter world: ``bump`` increments, the ``skip`` fault drops the first bump."""

    def __init__(self):
        self._trace, self._state, self._calls = [], {"count": 0}, 0
        self._fault, self._fired, self._closed = False, False, False

    def describe(self):
        from ghostlab.extensions.environment import (EnvironmentDescription, FaultSpec,
                                                     ToolSpec)
        return EnvironmentDescription(
            world_version="stub-p1/1", environment_id="stub", environment_version="stub/1",
            tools=[ToolSpec(name="bump", effect="mutate", parameters={})],
            faults=[FaultSpec(fault_id="skip", trigger_points=("first_bump",))],
            invariants=["counted"], fixtures=["zero"],
            capabilities={"snapshot_restore": False}, source="p1-test-stub")

    def named_fixture(self, name):
        if name != "zero":
            raise KeyError(name)
        return {"start": 0}

    def reset(self, fixture, scenario, *, actor_id):
        from ghostlab.extensions.environment import Observation
        self._trace = []
        self._state = {"count": fixture["start"]}
        self._calls = 0
        self._fault, self._fired, self._closed = bool(scenario["skip_first"]), False, False
        return Observation(trace=[], state=dict(self._state), calls=0)

    def execute(self, action):
        from ghostlab.extensions.environment import Outcome
        if self._closed:
            raise RuntimeError("closed")
        self._calls += 1
        if self._fault and not self._fired:
            self._fired = True
            outcome, recorded = ({"error": "Skipped", "message": "dropped"},
                                 Outcome(status="error", event_id=self._calls,
                                         error="Skipped", message="dropped"))
        else:
            self._state = {"count": self._state["count"] + 1}
            outcome, recorded = ({"result": dict(self._state)},
                                 Outcome(status="ok", event_id=self._calls,
                                         result=dict(self._state)))
        self._trace.append({"event_id": self._calls,
                            "request": {"tool": action.tool,
                                        "arguments": dict(action.arguments)},
                            "outcome": outcome, "state": dict(self._state)})
        return recorded

    def observe(self):
        from ghostlab.extensions.environment import Observation
        if self._closed:
            raise RuntimeError("closed")
        return Observation(trace=[dict(event) for event in self._trace],
                           state=dict(self._state), calls=self._calls)

    def evaluate(self, *, trace, state):
        from ghostlab.extensions.environment import InvariantCheck
        skipped = [event["event_id"] for event in trace
                   if event["outcome"].get("error") == "Skipped"]
        return [InvariantCheck(name="counted", passed=not skipped,
                               evidence_event_ids=tuple(skipped),
                               observed=state["count"], limit=1000, unit="bumps")]

    def faults_fired(self, scenario, trace):
        fired = any(event["outcome"].get("error") == "Skipped" for event in trace)
        return frozenset({"skip"}) if fired else frozenset()

    def close(self):
        self._closed = True

    def snapshot(self):
        from ghostlab.extensions.environment import UnsupportedOperation
        raise UnsupportedOperation("stub has no snapshots")

    def restore(self, snapshot):
        from ghostlab.extensions.environment import UnsupportedOperation
        raise UnsupportedOperation("stub has no snapshots")


class StubTarget:
    """Proposes three bumps, then finishes."""

    def __init__(self, adapter_id):
        self._adapter_id = adapter_id
        self._left, self._closed = 3, None

    def info(self):
        from ghostlab.extensions.target import TargetInfo
        return TargetInfo(adapter_id=self._adapter_id, adapter_version="stub/1",
                          model=None, provider=None, endpoint=None, actions=())

    def start(self, task, observation):
        self._task = task

    def propose(self, observation):
        from ghostlab.extensions.environment import Action
        if self._left <= 0:
            return None
        self._left -= 1
        return Action(tool="bump", arguments={})

    def notify_result(self, action, outcome):
        pass

    def close(self, reason):
        self._closed = reason


def stub_binding():
    from ghostlab.extensions.runner import run_attempt

    def minimum_cap(fixture):
        lengths = []
        for adapter_id in ("plain", "careful"):
            env = StubEnvironment()
            env.reset(dict(fixture), {"skip_first": True, "max_tool_calls": 10},
                      actor_id="tester")
            result = run_attempt(env, StubTarget(adapter_id),
                                 TargetTask(task_id="probe", params={}),
                                 authorize=lambda action: None, cap=10,
                                 cooperative_stop=False)
            lengths.append(len(result.trace))
        return min(lengths)

    def execute_request(services, fixture, request, *, scope, project_id, should_stop):
        def reset_environment(env):
            env.reset(dict(fixture), dict(request.scenario), actor_id="tester")

        def assemble(result):
            checks = {check.name: check.passed
                      for check in StubEnvironment().evaluate(trace=result.trace,
                                                             state=result.state)}
            return {"world_version": "stub-p1/1", "adapter_id": request.adapter,
                    "status": result.status, "trace": result.trace,
                    "final_state": result.state, "checks": checks}

        return execute_with_runner(
            target_factory=lambda: StubTarget(request.adapter),
            environment_factory=StubEnvironment, reset_environment=reset_environment,
            build_task=lambda: TargetTask(task_id="stub", params={}),
            authorize_action=lambda action: None, assemble_run=assemble,
            cap=request.cap, should_stop=should_stop, cooperative_stop=True)

    def validate_run(run):
        assert set(run) == {"world_version", "adapter_id", "status", "trace",
                            "final_state", "checks"}
        assert run["status"] in ("completed", "budget_exhausted")

    def assemble_diff(fixture, baseline, fork):
        failed = [name for name, passed in fork["checks"].items() if not passed]
        return {"first_divergence": first_divergence(baseline["trace"], fork["trace"]),
                "baseline_count": baseline["final_state"]["count"],
                "faulted_count": fork["final_state"]["count"],
                "violated_invariants": failed}

    def derive_findings(fixture, run_id, run, recorded_at, execution):
        return [{"finding_id": f"{run_id}-{name}", "run_id": run_id, "invariant": name,
                 "verdict": "passed" if passed else "failed",
                 "evidence_event_ids": [] if passed else [1],
                 "recorded_at": recorded_at}
                for name, passed in sorted(run["checks"].items())]

    def export_capsule(fixture, scenario, run, execution, path):
        path.write_text(json.dumps({"fixture": fixture, "scenario": scenario, "run": run}),
                        encoding="utf-8")

    def replay_capsule(path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        env = StubEnvironment()
        env.reset(payload["fixture"], payload["scenario"], actor_id="tester")
        replayed = replay_trace(env, payload["run"]["trace"])
        matches = {"trace": list(replayed.trace) == payload["run"]["trace"],
                   "final_state": dict(replayed.state) == payload["run"]["final_state"]}
        return {"reproduced": all(matches.values()), "matches": matches, "model_calls": 0}

    return ExtensionBinding(
        world_version="stub-p1/1", skill_id="stub-skill", spec_model=object,
        record_model=object, proposal_model=object, output_schema="stub_proposal",
        repair_summary="stub repair",
        current_fixture=lambda services: {"start": 0},
        snapshot_fixture=lambda fixture: dict(fixture),
        restore_fixture=lambda snapshot: {"start": snapshot["start"]},
        supervisor=lambda project_id, principal: "supervisor",
        target_scope=lambda project_id, principal: "target",
        problems=lambda proposal: [],
        explored=lambda proposal: f"adapter={proposal['adapter_id']}",
        identity_problem=lambda services, fixture, project_id, principal: None,
        faults_fired=lambda proposal, run: StubEnvironment().faults_fired({}, run["trace"]),
        faults_triggered=lambda proposal, fork: (
            StubEnvironment().faults_fired({}, fork["trace"])
            == ({"skip"} if proposal["scenario"]["skip_first"] else set())),
        minimum_cap=minimum_cap,
        explorer_facts=lambda fixture: (),
        experiment_runs=lambda proposal: [
            RunRequest({"skip_first": False,
                        "max_tool_calls": proposal["scenario"]["max_tool_calls"]},
                       proposal["adapter_id"],
                       proposal["scenario"]["max_tool_calls"], None),
            RunRequest(dict(proposal["scenario"]), proposal["adapter_id"],
                       proposal["scenario"]["max_tool_calls"], None)],
        execute_request=execute_request, validate_run=validate_run,
        assemble_diff=assemble_diff,
        run_summary=lambda diff: f"{diff['baseline_count']} vs {diff['faulted_count']}",
        derive_findings=derive_findings,
        finding_text=lambda finding: finding["finding_id"],
        repair_candidate=lambda proposal: proposal["adapter_id"] == "plain",
        repair_action=lambda experiment_id, proposal: {
            "kind": "apply_repair", "experiment_id": experiment_id, "adapter": "careful",
            "world_version": "stub-p1/1", "scenario": proposal["scenario"]},
        repair_request=lambda action: RunRequest(
            dict(action["scenario"]), action["adapter"],
            action["scenario"]["max_tool_calls"], None),
        assess_repair=lambda fixture, run, findings, execution: {
            "run_status": run["status"],
            "safety_held": all(item["verdict"] == "passed" for item in findings),
            "verified": run["status"] == "completed" and all(
                item["verdict"] == "passed" for item in findings)},
        repair_result=lambda run, action, verified: {"count": run["final_state"]["count"]},
        baseline_scenario=lambda proposal: {**proposal["scenario"], "skip_first": False},
        export_capsule=export_capsule, replay_capsule=replay_capsule)


def test_stub_world_runs_through_the_generic_driver(tmp_path):
    assert "stub-p1/1" not in DEFAULT_WORLDS
    driver = ExtensionDriver(stub_binding())
    proposal = {"adapter_id": "plain",
                "scenario": {"skip_first": True, "max_tool_calls": 10}}
    assert driver.problems(proposal) == []
    assert driver.minimum_cap({"start": 0}) == 3
    assert driver.cap(proposal) == 10
    runs = driver.experiment_runs(proposal)
    outcomes = [driver.execute(SimpleNamespace(), {"start": 0}, request, scope="target",
                               project_id="p", should_stop=lambda: None)
                for request in runs]
    assert [outcome.denial for outcome in outcomes] == [None, None]
    baseline, fork = (outcome.run for outcome in outcomes)
    driver.validate_run(baseline)
    driver.validate_run(fork)
    assert baseline["checks"] == {"counted": True}
    assert fork["checks"] == {"counted": False}
    diff = driver.diff({"start": 0}, baseline, fork)
    assert diff["first_divergence"]["event_id"] == 1
    assert diff["violated_invariants"] == ["counted"]
    assert driver.run_summary(diff) == "3 vs 2"
    findings = driver.findings({"start": 0}, "run-1", fork, RECORDED_AT, None)
    assert [item["verdict"] for item in findings] == ["failed"]
    assert driver.repair_candidate(proposal) is True
    assert driver.baseline_scenario(proposal) == {"skip_first": False, "max_tool_calls": 10}
    capsule = tmp_path / "stub.json"
    driver.capsule({"start": 0}, proposal["scenario"], fork, None, capsule)
    report = driver.replay(capsule)
    assert report["reproduced"] is True and report["model_calls"] == 0


def test_new_world_needs_no_registry_or_controller_change():
    register = default_registry()
    assert "stub-p1/1" not in register.worlds()
    assert "stub-p1/1" not in DEFAULT_WORLDS