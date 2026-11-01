"""One driver class for every registered world (``ghostlab-orchestration/2``).

:class:`ExtensionDriver` contains no world-specific conditions: every world
answer comes from its :class:`ExtensionBinding` -- data plus callbacks
supplied by a reviewed local adapter package. Adding a world registers a
binding; the controller and this driver do not change.
"""

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ExtensionBinding:
    """Everything the generic driver needs from one world.

    Each callback mirrors one driver method; signatures (``proposal`` and
    ``run`` are validated plain-data dicts)::

      current_fixture(services) -> fixture object
      snapshot_fixture(fixture) -> JSON dict pinned at campaign start
      restore_fixture(snapshot) -> fixture (raises ValueError/TypeError when corrupt)
      supervisor(project_id, principal) -> trusted supervisor scope
      target_scope(project_id, principal) -> trusted target scope
      problems(proposal) -> list[str] (empty when the proposal fits the world)
      explored(proposal) -> str (one line for the explorer's memory)
      identity_problem(services, fixture, project_id, principal) -> str | None
      faults_fired(proposal, run) -> set[str] (faults that demonstrably fired)
      faults_triggered(proposal, fork_run) -> bool (coverage: the fork met its faults)
      minimum_cap(fixture) -> int (smallest tool cap that can complete a run)
      explorer_facts(fixture) -> tuple[str, ...] (pinned-fixture facts for the explorer)
      experiment_runs(proposal) -> list[RunRequest] (baseline first, then fork)
      execute_request(services, fixture, request, *, scope, project_id, should_stop)
        -> RunOutcome (one evidence run; denial/stop runs carry no run)
      validate_run(run) -> None (raises when the run record is invalid)
      assemble_diff(fixture, baseline_run, fork_run) -> diff dict
      run_summary(diff) -> str (one line for the run-completed event)
      derive_findings(fixture, run_id, run, recorded_at, execution) -> list[dict]
      finding_text(finding) -> str (one line for memory)
      repair_candidate(proposal) -> bool (a checked-in repair exists to try)
      repair_action(experiment_id, proposal) -> approved action dict
      repair_request(action) -> RunRequest (from policy; the action must agree)
      assess_repair(fixture, run, findings, execution) -> dict with at least
        run_status, safety_held and verified
      repair_result(run, action, verified) -> dict (world-owned repair evidence)
      baseline_scenario(proposal) -> scenario dict (what the baseline recorded)
      export_capsule(fixture, scenario, run, execution, path) -> None
      replay_capsule(path) -> replay report dict (no model call)
    """

    world_version: str
    skill_id: str
    spec_model: Any
    record_model: Any
    proposal_model: Any
    output_schema: str
    repair_summary: str
    current_fixture: Callable
    snapshot_fixture: Callable
    restore_fixture: Callable
    supervisor: Callable
    target_scope: Callable
    problems: Callable
    explored: Callable
    identity_problem: Callable
    faults_fired: Callable
    faults_triggered: Callable
    minimum_cap: Callable
    explorer_facts: Callable
    experiment_runs: Callable
    execute_request: Callable
    validate_run: Callable
    assemble_diff: Callable
    run_summary: Callable
    derive_findings: Callable
    finding_text: Callable
    repair_candidate: Callable
    repair_action: Callable
    repair_request: Callable
    assess_repair: Callable
    repair_result: Callable
    baseline_scenario: Callable
    export_capsule: Callable
    replay_capsule: Callable


class ExtensionDriver:
    """Generic driver: delegates every world answer to its binding.

    The only structural rule here is :meth:`cap`: every proposal shape
    carries its tool budget at ``proposal["scenario"]["max_tool_calls"]``.
    """

    def __init__(self, binding: ExtensionBinding):
        if not isinstance(binding, ExtensionBinding):
            raise TypeError("ExtensionDriver needs an ExtensionBinding")
        self.binding = binding
        self.world_version = binding.world_version
        self.skill_id = binding.skill_id
        self.spec_model = binding.spec_model
        self.record_model = binding.record_model
        self.proposal_model = binding.proposal_model
        self.output_schema = binding.output_schema
        self.repair_summary = binding.repair_summary

    def current_fixture(self, services):
        return self.binding.current_fixture(services)

    def snapshot(self, fixture) -> dict:
        return self.binding.snapshot_fixture(fixture)

    def restore(self, snapshot: dict):
        return self.binding.restore_fixture(snapshot)

    def supervisor(self, project_id: str, principal):
        return self.binding.supervisor(project_id, principal)

    def target(self, project_id: str, principal):
        return self.binding.target_scope(project_id, principal)

    def cap(self, proposal: dict) -> int:
        return proposal["scenario"]["max_tool_calls"]

    def problems(self, proposal: dict) -> list[str]:
        return self.binding.problems(proposal)

    def explored(self, proposal: dict) -> str:
        return self.binding.explored(proposal)

    def identity_problem(self, services, fixture, project_id: str, principal):
        return self.binding.identity_problem(services, fixture, project_id, principal)

    def faults_fired(self, proposal: dict, run: dict) -> set[str]:
        return self.binding.faults_fired(proposal, run)

    def faults_triggered(self, proposal: dict, fork: dict) -> bool:
        return self.binding.faults_triggered(proposal, fork)

    def minimum_cap(self, fixture) -> int:
        return self.binding.minimum_cap(fixture)

    def explorer_facts(self, fixture) -> tuple[str, ...]:
        return self.binding.explorer_facts(fixture)

    def experiment_runs(self, proposal: dict) -> list:
        return self.binding.experiment_runs(proposal)

    def execute(self, services, fixture, request, *, scope, project_id: str,
                should_stop):
        return self.binding.execute_request(services, fixture, request, scope=scope,
                                            project_id=project_id, should_stop=should_stop)

    def validate_run(self, run: dict) -> None:
        self.binding.validate_run(run)

    def diff(self, fixture, baseline: dict, fork: dict) -> dict:
        return self.binding.assemble_diff(fixture, baseline, fork)

    def run_summary(self, diff: dict) -> str:
        return self.binding.run_summary(diff)

    def findings(self, fixture, run_id: str, run: dict, recorded_at: str,
                 execution) -> list[dict]:
        return self.binding.derive_findings(fixture, run_id, run, recorded_at, execution)

    def finding_text(self, finding: dict) -> str:
        return self.binding.finding_text(finding)

    def repair_candidate(self, proposal: dict) -> bool:
        return self.binding.repair_candidate(proposal)

    def repair_action(self, experiment_id: str, proposal: dict) -> dict:
        return self.binding.repair_action(experiment_id, proposal)

    def repair_request(self, action: dict):
        return self.binding.repair_request(action)

    def assess_repair(self, fixture, run: dict, findings: list, execution) -> dict:
        return self.binding.assess_repair(fixture, run, findings, execution)

    def repair_result(self, run: dict, action: dict, verified: bool) -> dict:
        return self.binding.repair_result(run, action, verified)

    def baseline_scenario(self, proposal: dict) -> dict:
        return self.binding.baseline_scenario(proposal)

    def capsule(self, fixture, scenario: dict, run: dict, execution, path) -> None:
        self.binding.export_capsule(fixture, scenario, run, execution, path)

    def replay(self, path) -> dict:
        return self.binding.replay_capsule(path)