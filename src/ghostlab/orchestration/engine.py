"""Run the trusted refund world under M06 tool authorization, then derive findings.

``ghostlab.core`` (M02) is used through its public classes and is not edited.
``PolicyEnforcedWorld`` asks M06 before every tool call: a denied call raises
before ``RefundWorld.call``, so it leaves no trace event and no ledger change.
For allowed calls the result is identical to ``ghostlab.core.run`` (tested).
The world's actor comes from the controller's ``PrincipalBinding``, never from a
fixture default or a tool argument.
"""

from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from ghostlab.contracts import InvariantFinding, TrustedScope
from ghostlab.contracts.tools import ToolCall
from ghostlab.core import (ADAPTER_VERSION, BudgetExceeded, Fixture, RefundWorld, Scenario,
                           ToolError, run_agent, verify)
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
    run: dict | None          # RunResult-shaped dict, or None when policy denied the run
    denial: Decision | None
    tool_calls: int


def execute_run(fixture: Fixture, scenario: Scenario, adapter: str, *, scope: TrustedScope,
                project_id: str, binding: PrincipalBinding) -> RunOutcome:
    """``ghostlab.core.run`` with every tool call authorized through M06."""
    world = PolicyEnforcedWorld(fixture, scenario, scope=scope, project_id=project_id,
                                binding=binding)
    status = "completed"
    try:
        run_agent(world, adapter)
    except BudgetExceeded:
        status = "budget_exhausted"
    except ToolDenied as denied:
        return RunOutcome(None, denied.decision, world.calls)
    state = world.snapshot()
    return RunOutcome({"adapter": adapter, "adapter_version": ADAPTER_VERSION,
                       "actor_id": world.actor_id, "status": status, "trace": world.trace,
                       "final_state": state, "checks": verify(fixture, state)},
                      None, world.calls)


def ledger_change_events(run: dict) -> list[int]:
    """Event ids at which the authoritative ledger grew (the evidence for refund checks)."""
    events, size = [], 0
    for event in run["trace"]:
        grown = len(event["state"]["ledger"])
        if grown > size:
            events.append(event["event_id"])
            size = grown
    return events


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
