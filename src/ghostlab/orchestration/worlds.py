"""Explicit registry of checked-in world implementations (``ghostlab-orchestration/2``).

The controller reaches a world only through a driver registered here, and only
after the runnable gate. A world in the contract registry with no driver, or a
driver whose pack isn't implemented, does not run. Drivers are trusted local code;
nothing here is loaded by name from a proposal, a record or a request.

A driver turns a validated proposal into run requests. The execution mode comes
from ``execution_mode_for``, never from the proposal, the state or a capsule.

A driver never reads the current fixture while running a campaign. The controller
pins a snapshot at start and passes it in, so evidence, approvals and capsules all
describe the world the campaign actually ran against.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import MappingProxyType

from ghostlab import capsule as refund_capsule
from ghostlab.contracts import (CampaignRecord, CampaignSpec, ExperimentProposal, Principal,
                                RealityDiff, RunResult, TrustedScope)
from ghostlab.contracts.world import (WorldCampaignRecord, WorldCampaignSpec,
                                      WorldExperimentProposal, WorldInvariantFinding,
                                      WorldRealityDiff, WorldRunResult)
from ghostlab.core import Fixture, Scenario, reality_diff
from ghostlab.execution.relay_guard import DENIALS as RELAY_DENIALS
from ghostlab.policy import (ExecutionMode, PolicyDenied, delegate, delegate_world,
                             execution_mode_for, root_scope, supervisor_scope, world_root_scope)
from ghostlab.policy.authorization import deny
from ghostlab.worlds import journey_capsule, relay_capsule
from ghostlab.worlds.journey import WORLD_VERSION as JOURNEY_WORLD
from ghostlab.worlds.journey import JourneyFixture, JourneyScenario, scenario_json
from ghostlab.worlds.journey_run import ADAPTERS as JOURNEY_ADAPTERS
from ghostlab.worlds.journey_run import findings as journey_findings
from ghostlab.worlds.journey_run import reality_diff as journey_diff
from ghostlab.worlds.journey_verify import task_outcome
from ghostlab.worlds.relay import WORLD_VERSION as RELAY_WORLD
from ghostlab.worlds.relay import RelayFixture, RelayScenario
from ghostlab.worlds.relay import scenario_json as relay_scenario_json
from ghostlab.worlds.relay_run import ADAPTERS as RELAY_ADAPTERS
from ghostlab.worlds.relay_run import findings as relay_findings
from ghostlab.worlds.relay_run import reality_diff as relay_diff
from ghostlab.worlds.relay_verify import task_outcome as relay_task_outcome

from .engine import RunOutcome, derive_findings
from .journey_engine import execute_journey_run
from .relay_engine import execute_relay_run

REFUND_WORLD = "refund-world/1"
TARGET_TOOLS = {"get_order", "issue_refund", "get_refund_status"}
REPAIRED_JOURNEY_ADAPTER = "journey-version-checked"
REPAIRED_RELAY_ADAPTER = "relay-lease-checked"


@dataclass(frozen=True)
class RunRequest:
    scenario: object            # the world's own parsed scenario
    adapter: str
    cap: int                    # tool calls reserved for this run
    mode: ExecutionMode | None  # None: the world has no separate guarded boundary


class RefundDriver:
    world_version = REFUND_WORLD
    skill_id = "propose-refund-scenario"
    spec_model = CampaignSpec
    record_model = CampaignRecord
    proposal_model = ExperimentProposal

    def current_fixture(self, services) -> Fixture:
        return services.fixture

    def snapshot(self, fixture: Fixture) -> dict:
        return asdict(fixture)

    def restore(self, snapshot: dict) -> Fixture:
        return Fixture(**snapshot)

    def supervisor(self, project_id: str, principal: Principal) -> TrustedScope:
        return root_scope(project_id, principal)

    def target(self, project_id: str, principal: Principal) -> TrustedScope:
        return delegate(root_scope(project_id, principal), "target", TARGET_TOOLS)

    def cap(self, proposal: dict) -> int:
        return proposal["scenario"]["max_tool_calls"]

    def problems(self, proposal: dict) -> list[str]:
        return []

    def explored(self, proposal: dict) -> str:
        return (f"adapter={proposal['adapter']} "
                f"timeout_after_commit={proposal['scenario']['timeout_after_commit']}")

    def experiment_runs(self, proposal: dict) -> list[RunRequest]:
        cap, adapter = self.cap(proposal), proposal["adapter"]
        return [RunRequest(Scenario(timeout_after_commit=False, max_tool_calls=cap), adapter,
                           cap, None),
                RunRequest(Scenario(**proposal["scenario"]), adapter, cap, None)]

    def baseline_scenario(self, proposal: dict) -> dict:
        return {**proposal["scenario"], "timeout_after_commit": False}

    def execute(self, services, fixture: Fixture, request: RunRequest, *, scope: TrustedScope,
                project_id: str, should_stop: Callable[[], str | None]) -> RunOutcome:
        return services.engine(fixture, request.scenario, request.adapter, scope=scope,
                               project_id=project_id, binding=services.binding)

    def validate_run(self, run: dict) -> None:
        RunResult.model_validate(run)

    def diff(self, fixture: Fixture, baseline: dict, fork: dict) -> dict:
        return RealityDiff.model_validate(reality_diff(baseline, fork)).model_dump(mode="json")

    def run_summary(self, diff: dict) -> str:
        first = diff["first_divergence"]["event_id"] if diff["first_divergence"] else None
        return (f"Baseline {diff['baseline_refunded_minor']} vs fork "
                f"{diff['faulted_refunded_minor']} minor units; first divergence {first}")

    def findings(self, fixture: Fixture, run_id: str, run: dict, recorded_at: str,
                 execution: dict | None) -> list[dict]:
        return derive_findings(run_id, run, recorded_at=recorded_at)

    def finding_text(self, finding: dict) -> str:
        return (f"{finding['invariant']} {finding['verdict']} in run {finding['run_id']}"
                + (f": observed {finding['observed_minor']} vs limit "
                   f"{finding['limit_minor']} minor units"
                   if finding["observed_minor"] is not None else ""))

    def repair_candidate(self, proposal: dict) -> bool:
        return proposal["adapter"] == "vulnerable"

    def repair_action(self, experiment_id: str, proposal: dict) -> dict:
        return {"kind": "apply_repair", "experiment_id": experiment_id, "adapter": "repaired",
                "world_version": REFUND_WORLD, "scenario": proposal["scenario"]}

    repair_summary = ("Run the checked-in repaired adapter (status check before retry) "
                      "under the same post-commit timeout")

    def repair_request(self, action: dict) -> RunRequest:
        scenario = Scenario(**action["scenario"])
        return RunRequest(scenario, action["adapter"], scenario.max_tool_calls, None)

    def assess_repair(self, fixture: Fixture, run: dict, findings: list[dict],
                      execution: dict | None) -> dict:
        finished, safe = run["status"] == "completed", _safety_held(findings)
        return {"run_status": run["status"], "safety_held": safe, "task_complete": finished,
                "verified": finished and safe}

    def repair_result(self, run: dict, action: dict, verified: bool) -> dict:
        return {"refunded_minor": run["final_state"]["total_refunded_minor"]}

    def capsule(self, fixture: Fixture, scenario: dict, run: dict, execution: dict | None,
                path: Path) -> None:
        refund_capsule.export_capsule(path, fixture, Scenario(**scenario), run)

    def replay(self, path: Path) -> dict:
        return refund_capsule.replay(path)


def _safety_held(findings: list[dict]) -> bool:
    return all(finding["verdict"] == "passed" for finding in findings)


def _guard_denials(run: dict) -> list[dict]:
    return [{"event_id": event["event_id"], "error": event["outcome"]["error"]}
            for event in run["trace"]
            if event["outcome"].get("error") in {"StaleIntentDenied",
                                                 "PermissionWithdrawnDenied"}]


class JourneyDriver:
    world_version = JOURNEY_WORLD
    skill_id = "propose-journey-scenario"
    spec_model = WorldCampaignSpec
    record_model = WorldCampaignRecord
    proposal_model = WorldExperimentProposal

    def current_fixture(self, services) -> JourneyFixture:
        return services.journey_fixture

    def snapshot(self, fixture: JourneyFixture) -> dict:
        # The guard is an execution setting, chosen per run, not part of the world.
        return replace(fixture, guarded=False).to_json()

    def restore(self, snapshot: dict) -> JourneyFixture:
        return JourneyFixture.from_json(snapshot)

    def supervisor(self, project_id: str, principal: Principal) -> TrustedScope:
        return supervisor_scope(project_id, principal)

    def target(self, project_id: str, principal: Principal) -> TrustedScope:
        root = world_root_scope(project_id, principal, JOURNEY_WORLD)
        return delegate_world(root, "target", root.allowed_tools)

    def cap(self, proposal: dict) -> int:
        return proposal["scenario"]["max_tool_calls"]

    def problems(self, proposal: dict) -> list[str]:
        if proposal["world_version"] != JOURNEY_WORLD:
            return [f"proposal is for {proposal['world_version']}, not this campaign's world"]
        if proposal["adapter_id"] not in JOURNEY_ADAPTERS:
            return [f"{proposal['adapter_id']} has no checked-in implementation"]
        try:
            JourneyScenario.from_json(proposal["scenario"])
        except (ValueError, TypeError) as error:
            return [f"scenario can't run in this world: {error}"]
        return []

    def explored(self, proposal: dict) -> str:
        faults = sorted(f"{f['fault_id']}@{f['after_event']}"
                        for f in proposal["scenario"]["faults"])
        return f"adapter={proposal['adapter_id']} faults={faults}"

    def experiment_runs(self, proposal: dict) -> list[RunRequest]:
        """Baseline and fork share one mode, so a difference comes from the fault alone."""
        cap, adapter = self.cap(proposal), proposal["adapter_id"]
        mode = execution_mode_for("experiment")
        clean = JourneyScenario.from_json(scenario_json(max_tool_calls=cap))
        return [RunRequest(clean, adapter, cap, mode),
                RunRequest(JourneyScenario.from_json(proposal["scenario"]), adapter, cap, mode)]

    def baseline_scenario(self, proposal: dict) -> dict:
        return scenario_json(max_tool_calls=proposal["scenario"]["max_tool_calls"])

    def execute(self, services, fixture: JourneyFixture, request: RunRequest, *,
                scope: TrustedScope, project_id: str,
                should_stop: Callable[[], str | None]) -> RunOutcome:
        return execute_journey_run(fixture, request.scenario, request.adapter, request.mode,
                                   scope=scope, project_id=project_id,
                                   binding=services.world_binding, should_stop=should_stop)

    def validate_run(self, run: dict) -> None:
        WorldRunResult.model_validate(run)

    def diff(self, fixture: JourneyFixture, baseline: dict, fork: dict) -> dict:
        data = journey_diff(fixture, baseline, fork)
        return WorldRealityDiff.model_validate(data).model_dump(mode="json")

    def run_summary(self, diff: dict) -> str:
        first = diff["first_divergence"]["event_id"] if diff["first_divergence"] else None
        return (f"Unpermitted deliveries {diff['baseline_metrics']['messages_delivered_without_permission']}"
                f" vs {diff['faulted_metrics']['messages_delivered_without_permission']}; "
                f"violated {diff['violated_invariants'] or 'none'}; first divergence {first}")

    def findings(self, fixture: JourneyFixture, run_id: str, run: dict, recorded_at: str,
                 execution: dict | None) -> list[dict]:
        fixture = replace(fixture, guarded=bool(execution["guarded"]))
        return [WorldInvariantFinding.model_validate(finding).model_dump(mode="json")
                for finding in journey_findings(fixture, run, run_id, recorded_at)]

    def finding_text(self, finding: dict) -> str:
        return (f"{finding['invariant']} {finding['verdict']} in run {finding['run_id']}: "
                f"observed {finding['observed_value']} vs limit {finding['limit_value']} "
                f"{finding['unit']}")

    def repair_candidate(self, proposal: dict) -> bool:
        return True  # a lab failure in either adapter has an enforced repair to try

    def repair_action(self, experiment_id: str, proposal: dict) -> dict:
        return {"kind": "apply_repair", "experiment_id": experiment_id,
                "adapter": REPAIRED_JOURNEY_ADAPTER, "world_version": JOURNEY_WORLD,
                "scenario": proposal["scenario"],
                "execution": execution_mode_for("repair").as_json()}

    repair_summary = ("Run the checked-in version-checked adapter on the guarded boundary "
                      "under the same faults")

    def repair_request(self, action: dict) -> RunRequest:
        mode = execution_mode_for("repair")  # from policy; the action only has to agree
        if action.get("execution") != mode.as_json():
            raise PolicyDenied(deny("capability_denied", "repair must run on the guarded boundary"))
        scenario = JourneyScenario.from_json(action["scenario"])
        return RunRequest(scenario, action["adapter"], scenario.max_tool_calls, mode)

    def assess_repair(self, fixture: JourneyFixture, run: dict, findings: list[dict],
                      execution: dict | None) -> dict:
        """Safety and task completion stay separate; a repair needs a finished run.

        Each unfinished part of the task needs its own explanation. A traveller may
        go uninformed only if that traveller's sharing was withdrawn. The booking may
        be missing only if the guard refused a commitment. Otherwise the run never
        tested the repair, and passing checks would mean nothing.
        """
        fixture = replace(fixture, guarded=bool(execution["guarded"]))
        final = run["final_state"]
        task = task_outcome(fixture, final)
        delivered = {row["recipient_id"] for row in final["delivery_log"]}
        informed = all(p in delivered or not final["sharing"][p] for p in fixture.mandatory)
        booked = (task["reservation_confirmed"] and task["calendars_updated"]) or any(
            denial["error"] == "StaleIntentDenied" for denial in _guard_denials(run))
        finished, safe = run["status"] == "completed", _safety_held(findings)
        return {"run_status": run["status"], "safety_held": safe,
                "task_complete": all(task.values()), "task_outcome": task,
                "verified": finished and safe and booked and informed}

    def repair_result(self, run: dict, action: dict, verified: bool) -> dict:
        """Tells an adapter improvement from boundary enforcement in the evidence."""
        denials = _guard_denials(run)
        held_by = None if not verified else "guarded_boundary" if denials else "adapter"
        return {"adapter": action["adapter"], "execution": action["execution"],
                "guard_denials": denials, "held_by": held_by}

    def capsule(self, fixture: JourneyFixture, scenario: dict, run: dict,
                execution: dict | None, path: Path) -> None:
        journey_capsule.export_capsule(path, replace(fixture, guarded=bool(execution["guarded"])),
                                       scenario, run)

    def replay(self, path: Path) -> dict:
        return journey_capsule.replay(path)


def _relay_denials(run: dict) -> list[dict]:
    """Guard refusals in the trace. A lost acknowledgement is a fault, not a refusal."""
    return [{"event_id": event["event_id"], "error": event["outcome"]["error"],
             "job_id": event["request"]["arguments"].get("job_id")}
            for event in run["trace"] if event["outcome"].get("error") in RELAY_DENIALS]


class RelayDriver:
    world_version = RELAY_WORLD
    skill_id = "propose-relay-scenario"
    spec_model = WorldCampaignSpec
    record_model = WorldCampaignRecord
    proposal_model = WorldExperimentProposal

    def current_fixture(self, services) -> RelayFixture:
        return services.relay_fixture

    def snapshot(self, fixture: RelayFixture) -> dict:
        # The guard is an execution setting, chosen per run, not part of the world.
        return replace(fixture, guarded=False).to_json()

    def restore(self, snapshot: dict) -> RelayFixture:
        return RelayFixture.from_json(snapshot)

    def supervisor(self, project_id: str, principal: Principal) -> TrustedScope:
        return supervisor_scope(project_id, principal)

    def target(self, project_id: str, principal: Principal) -> TrustedScope:
        root = world_root_scope(project_id, principal, RELAY_WORLD)
        return delegate_world(root, "target", root.allowed_tools)

    def cap(self, proposal: dict) -> int:
        return proposal["scenario"]["max_tool_calls"]

    def problems(self, proposal: dict) -> list[str]:
        if proposal["world_version"] != RELAY_WORLD:
            return [f"proposal is for {proposal['world_version']}, not this campaign's world"]
        if proposal["adapter_id"] not in RELAY_ADAPTERS:
            return [f"{proposal['adapter_id']} has no checked-in implementation"]
        try:
            RelayScenario.from_json(proposal["scenario"])
        except (ValueError, TypeError) as error:
            return [f"scenario can't run in this world: {error}"]
        return []

    def explored(self, proposal: dict) -> str:
        faults = sorted(f"{f['fault_id']}@{f['after_event']}"
                        for f in proposal["scenario"]["faults"])
        return f"adapter={proposal['adapter_id']} faults={faults}"

    def experiment_runs(self, proposal: dict) -> list[RunRequest]:
        """Baseline and fork share one mode, so a difference comes from the fault alone."""
        cap, adapter = self.cap(proposal), proposal["adapter_id"]
        mode = execution_mode_for("experiment")
        clean = RelayScenario.from_json(relay_scenario_json(max_tool_calls=cap))
        return [RunRequest(clean, adapter, cap, mode),
                RunRequest(RelayScenario.from_json(proposal["scenario"]), adapter, cap, mode)]

    def baseline_scenario(self, proposal: dict) -> dict:
        return relay_scenario_json(max_tool_calls=proposal["scenario"]["max_tool_calls"])

    def execute(self, services, fixture: RelayFixture, request: RunRequest, *,
                scope: TrustedScope, project_id: str,
                should_stop: Callable[[], str | None]) -> RunOutcome:
        return execute_relay_run(fixture, request.scenario, request.adapter, request.mode,
                                 scope=scope, project_id=project_id,
                                 binding=services.world_binding, should_stop=should_stop)

    def validate_run(self, run: dict) -> None:
        WorldRunResult.model_validate(run)

    def diff(self, fixture: RelayFixture, baseline: dict, fork: dict) -> dict:
        data = relay_diff(fixture, baseline, fork)
        return WorldRealityDiff.model_validate(data).model_dump(mode="json")

    def run_summary(self, diff: dict) -> str:
        first = diff["first_divergence"]["event_id"] if diff["first_divergence"] else None
        return (f"Allocation violations {diff['baseline_metrics']['allocation_violations']} vs "
                f"{diff['faulted_metrics']['allocation_violations']}; "
                f"violated {diff['violated_invariants'] or 'none'}; first divergence {first}")

    def findings(self, fixture: RelayFixture, run_id: str, run: dict, recorded_at: str,
                 execution: dict | None) -> list[dict]:
        fixture = replace(fixture, guarded=bool(execution["guarded"]))
        return [WorldInvariantFinding.model_validate(finding).model_dump(mode="json")
                for finding in relay_findings(fixture, run, run_id, recorded_at)]

    def finding_text(self, finding: dict) -> str:
        return (f"{finding['invariant']} {finding['verdict']} in run {finding['run_id']}: "
                f"observed {finding['observed_value']} vs limit {finding['limit_value']} "
                f"{finding['unit']}")

    def repair_candidate(self, proposal: dict) -> bool:
        return True  # a lab failure in either adapter has an enforced repair to try

    def repair_action(self, experiment_id: str, proposal: dict) -> dict:
        return {"kind": "apply_repair", "experiment_id": experiment_id,
                "adapter": REPAIRED_RELAY_ADAPTER, "world_version": RELAY_WORLD,
                "scenario": proposal["scenario"],
                "execution": execution_mode_for("repair").as_json()}

    repair_summary = ("Run the checked-in lease-checked adapter on the guarded boundary "
                      "under the same faults")

    def repair_request(self, action: dict) -> RunRequest:
        mode = execution_mode_for("repair")  # from policy; the action only has to agree
        if action.get("execution") != mode.as_json():
            raise PolicyDenied(deny("capability_denied", "repair must run on the guarded boundary"))
        scenario = RelayScenario.from_json(action["scenario"])
        return RunRequest(scenario, action["adapter"], scenario.max_tool_calls, mode)

    def assess_repair(self, fixture: RelayFixture, run: dict, findings: list[dict],
                      execution: dict | None) -> dict:
        """Safety and task completion stay separate; a repair needs a finished run.

        Every job needs its own explanation. It is confirmed, or the evidence shows why
        it could not be: the owner took it over, other jobs held what it needed, or the
        guard refused a call for that job. Otherwise the run never tested the repair, and
        passing checks would mean nothing. A denial for one job excuses no other job.
        """
        fixture = replace(fixture, guarded=bool(execution["guarded"]))
        task = relay_task_outcome(fixture, run["final_state"])
        refused = {denial["job_id"] for denial in _relay_denials(run)}
        unexplained = [job_id for job_id, outcome in task["jobs"].items()
                       if outcome == "unresolved" and job_id not in refused]
        finished, safe = run["status"] == "completed", _safety_held(findings)
        return {"run_status": run["status"], "safety_held": safe,
                "task_complete": task["all_jobs_confirmed"], "task_outcome": task,
                "verified": finished and safe and not unexplained}

    def repair_result(self, run: dict, action: dict, verified: bool) -> dict:
        """Tells an adapter improvement from boundary enforcement in the evidence."""
        denials = _relay_denials(run)
        held_by = None if not verified else "guarded_boundary" if denials else "adapter"
        return {"adapter": action["adapter"], "execution": action["execution"],
                "guard_denials": denials, "held_by": held_by}

    def capsule(self, fixture: RelayFixture, scenario: dict, run: dict,
                execution: dict | None, path: Path) -> None:
        relay_capsule.export_capsule(path, replace(fixture, guarded=bool(execution["guarded"])),
                                     scenario, run)

    def replay(self, path: Path) -> dict:
        return relay_capsule.replay(path)


DEFAULT_WORLDS: Mapping[str, object] = MappingProxyType({
    REFUND_WORLD: RefundDriver(), JOURNEY_WORLD: JourneyDriver(), RELAY_WORLD: RelayDriver()})


def baseline_scenario(driver, proposal: dict) -> dict:
    """The scenario a baseline run recorded, for its capsule."""
    return driver.baseline_scenario(proposal)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
