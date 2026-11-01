"""Run the trusted refund world under M06 tool authorization, then derive findings.

``ghostlab.core`` (M02) is used through its public classes and is not edited.
``execute_run`` executes behind the frozen extension interfaces: the checked-in
refund environment and target adapters plus the generic runner, with M06
deciding every proposed action before dispatch. A denied call never reaches the
world, so it leaves no trace event and no ledger change. For allowed calls the
result is identical to ``ghostlab.core.run`` (tested). The world's actor comes
from the controller's ``PrincipalBinding``, never from a fixture default or a
tool argument. ``PolicyEnforcedWorld`` remains available for direct authorized
calls.
"""

from dataclasses import asdict, dataclass

from pydantic import TypeAdapter, ValidationError

from ghostlab.adapters import (REFUND_WORLD_VERSION, default_registry,
                               ledger_change_events as _ledger_growth)
from ghostlab.contracts import InvariantFinding, TrustedScope
from ghostlab.contracts.tools import ToolCall
from ghostlab.core import (ADAPTER_VERSION, Fixture, RefundWorld, Scenario, ToolError, verify)
from ghostlab.extensions.registry import UnknownTargetError, UnknownWorldError
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.policy import Decision, PolicyDenied, PrincipalBinding, authorize_tool
from ghostlab.policy.authorization import deny

VERIFIER_VERSION = "refund-verifier-1"
_TOOL_CALL = TypeAdapter(ToolCall)


class ToolDenied(ToolError):
    """A world tool call refused by policy before it reached the world."""

    def __init__(self, decision: Decision):
        super().__init__(f"{decision.code}: {decision.reason}")
        self.decision = decision


class PolicyEnforcedWorld(RefundWorld):
    def __init__(self, fixture: Fixture, scenario: Scenario, *, scope: TrustedScope,
                 project_id: str, binding: PrincipalBinding):
        actor = binding.customer_id_for(project_id, scope.principal)
        if actor is None:
            raise PolicyDenied(deny("unbound_principal", "no world identity is bound"))
        super().__init__(fixture, scenario, actor_id=actor)
        self.scope, self.project_id, self.binding = scope, project_id, binding

    def call(self, tool, arguments):
        try:
            parsed = _TOOL_CALL.validate_python({"tool": tool, "arguments": arguments})
        except ValidationError:
            raise ToolDenied(deny("invalid_arguments", "arguments do not match the tool schema"))
        decision = authorize_tool(self.scope, parsed, project_id=self.project_id,
                                  binding=self.binding)
        if not decision.allowed:
            raise ToolDenied(decision)
        return super().call(tool, arguments)


@dataclass(frozen=True)
class RunOutcome:
    run: dict | None          # RunResult-shaped dict, or None when the run did not finish
    denial: Decision | None
    tool_calls: int
    stopped: str | None = None  # why a run stopped early: cancelled or out of wall clock


def execute_with_runner(*, target_factory, environment_factory, reset_environment,
                        build_task, authorize_action, assemble_run, cap: int,
                        should_stop=None, cooperative_stop: bool = True) -> RunOutcome:
    """One target-vs-environment attempt through the frozen extension interfaces.

    The world binding supplies every world answer -- factories, reset, task,
    per-action authorization, run assembly -- and this helper owns only the
    wiring: build the attempt, run the generic loop, map the outcome.
    Per-action denials become ``RunOutcome.denial``; a stop becomes
    ``RunOutcome.stopped``. Setup failures (unknown adapter, unbound actor)
    propagate to the caller.
    """
    target = target_factory()
    env = environment_factory()
    reset_environment(env)
    result = run_attempt(env, target, build_task(), authorize=authorize_action, cap=cap,
                         should_stop=should_stop, cooperative_stop=cooperative_stop)
    if result.denial is not None:
        return RunOutcome(None, result.denial, result.tool_calls)
    if result.stopped is not None:
        return RunOutcome(None, None, result.tool_calls, result.stopped)
    return RunOutcome(assemble_run(result), None, result.tool_calls)


def execute_run(fixture: Fixture, scenario: Scenario, adapter: str, *, scope: TrustedScope,
                project_id: str, binding: PrincipalBinding) -> RunOutcome:
    """``ghostlab.core.run`` with every tool call authorized through M06.

    Runs behind the frozen extension interfaces (see :func:`execute_with_runner`):
    the checked-in refund environment and target adapters plus the generic
    runner. Evidence is identical to the direct core run when every call is
    allowed (tested).
    """
    actor = binding.customer_id_for(project_id, scope.principal)
    if actor is None:
        raise PolicyDenied(deny("unbound_principal", "no world identity is bound"))
    registry = default_registry()
    try:
        target_factory = registry.target_factory(REFUND_WORLD_VERSION, adapter)
    except (UnknownWorldError, UnknownTargetError, TypeError):
        raise ValueError("Unknown adapter") from None
    environment_factory = registry.environment_factory(REFUND_WORLD_VERSION)
    fixture_snapshot, scenario_dict = asdict(fixture), asdict(scenario)

    def reset_environment(env):
        env.reset(fixture_snapshot, scenario_dict, actor_id=actor)

    def build_task():
        return TargetTask(task_id=f"refund-{adapter}",
                          params={"order_id": fixture.order_id,
                                  "customer_id": fixture.customer_id})

    def authorize_action(action):
        try:
            parsed = _TOOL_CALL.validate_python(
                {"tool": action.tool, "arguments": dict(action.arguments)})
        except ValidationError:
            return deny("invalid_arguments", "arguments do not match the tool schema")
        decision = authorize_tool(scope, parsed, project_id=project_id, binding=binding)
        return None if decision.allowed else decision

    def assemble_run(result):
        return {"adapter": adapter, "adapter_version": ADAPTER_VERSION, "actor_id": actor,
                "status": result.status, "trace": result.trace, "final_state": result.state,
                "checks": verify(fixture, result.state)}

    return execute_with_runner(
        target_factory=target_factory, environment_factory=environment_factory,
        reset_environment=reset_environment, build_task=build_task,
        authorize_action=authorize_action, assemble_run=assemble_run,
        cap=scenario.max_tool_calls, cooperative_stop=False)


def ledger_change_events(run: dict) -> list[int]:
    """Event ids at which the authoritative ledger grew (the evidence for refund checks)."""
    return _ledger_growth(run["trace"])


def derive_findings(run_id: str, run: dict, *, recorded_at: str) -> list[dict]:
    """One InvariantFinding per check, from ledger/state evidence; never model confidence."""
    evidence = ledger_change_events(run)
    state = run["final_state"]
    findings = []
    for invariant, passed in sorted(run["checks"].items()):
        totals = invariant == "total_refund_within_payment"
        finding = InvariantFinding.model_validate({
            "schema_version": 1, "finding_id": f"{run_id}-{invariant}", "run_id": run_id,
            "invariant": invariant, "verifier_version": VERIFIER_VERSION,
            "verdict": "passed" if passed else "failed",
            "evidence_event_ids": evidence if (not passed or totals) else [],
            "observed_minor": state["total_refunded_minor"] if totals else None,
            "limit_minor": state["order"]["paid_minor"] if totals else None,
            "recorded_at": recorded_at})
        findings.append(finding.model_dump(mode="json"))
    return findings
