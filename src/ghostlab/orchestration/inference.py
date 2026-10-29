"""Model access for logical roles: live-inference gate and the Explorer call.

Inference is mocked unless an explicit ``LiveInferenceAuthorization`` names the
exact model, a spending cap and a passing *live* M01 compatibility report for
that model. A runtime without a mock transport would reach the network, so the
controller refuses it otherwise. No test constructs a network runtime.
"""

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ghostlab.budgets import AttemptGate, BudgetAccountant
from ghostlab.context import ContextCompiler, ContextRequest, MemorySelection
from ghostlab.contracts import BudgetLimits, ExperimentProposal, ModelReply, ModelRequest
from ghostlab.providers import ProviderAdapter, Runtime

PASSING = {"passed"}


class LiveInferenceRefused(Exception):
    pass


@dataclass(frozen=True)
class LiveInferenceAuthorization:
    """Explicit operator authorization for real provider calls."""

    model: str
    max_model_calls: int          # spending cap, in HTTP attempts
    compatibility_report: Path    # M01 probe report from a live run
    confirm_paid_calls: bool

    def check(self, provider_model: str, campaign_limits: BudgetLimits):
        if not self.confirm_paid_calls:
            raise LiveInferenceRefused("paid calls were not confirmed")
        if self.model != provider_model:
            raise LiveInferenceRefused("authorization names a different model")
        if not 1 <= self.max_model_calls <= campaign_limits.max_model_calls:
            raise LiveInferenceRefused("spending cap must be 1..campaign max_model_calls")
        try:
            report = json.loads(self.compatibility_report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise LiveInferenceRefused("compatibility report is missing or unreadable") from error
        if report.get("live") is not True or report.get("transport") != "network":
            raise LiveInferenceRefused("compatibility report is not from a live run")
        if report.get("model") != self.model:
            raise LiveInferenceRefused("compatibility report is for a different model")
        checks = report.get("checks") or []
        if not checks or any(c.get("status") not in PASSING for c in checks):
            raise LiveInferenceRefused("compatibility report has failing or skipped checks")


def require_inference_allowed(runtime: Runtime, provider_model: str, limits: BudgetLimits,
                              live: LiveInferenceAuthorization | None) -> BudgetLimits:
    """Return the effective limits; refuse a network runtime without authorization."""
    if runtime.transport is not None:
        return limits  # mocked transport: no request leaves the process
    if live is None:
        raise LiveInferenceRefused("a network runtime needs LiveInferenceAuthorization")
    live.check(provider_model, limits)
    return limits.model_validate({**limits.model_dump(),
                                  "max_model_calls": live.max_model_calls})


@dataclass(frozen=True)
class ProposalOutcome:
    proposal: dict | None        # ExperimentProposal as JSON, when valid
    reply: dict                  # ModelReply as JSON (already credential-scrubbed by M01)
    invocation: dict             # skill/prompt versions used
    bundle_sha256: str
    attempts: int
    exhausted: str | None        # resource whose budget stopped the call


class ExplorerClient:
    """Explorer role: an isolated M03 context, one structured proposal, no tools."""

    def __init__(self, compiler: ContextCompiler,
                 adapter_factory: Callable[[Runtime], ProviderAdapter], runtime: Runtime, *,
                 max_output_tokens: int = 1_000, timeout_ms: int = 60_000,
                 token_budget: int = 8_000):
        self.compiler, self.adapter_factory, self.runtime = compiler, adapter_factory, runtime
        self.max_output_tokens, self.timeout_ms = max_output_tokens, timeout_ms
        self.token_budget = token_budget

    def propose(self, *, scope, campaign_id: str, task_id: str, request_id: str,
                objective: str, success_criteria: tuple[str, ...], as_of: str,
                questions: tuple[str, ...], accountant: BudgetAccountant,
                task_limits: BudgetLimits, cancel=None) -> ProposalOutcome:
        compiled = self.compiler.compile(ContextRequest(
            bundle_id=f"bundle-{task_id}", campaign_id=campaign_id, task_id=task_id,
            role="explorer", scope=scope, objective=objective,
            success_criteria=success_criteria, skill_id="propose-refund-scenario",
            as_of=as_of, compiled_at=as_of, token_budget=self.token_budget,
            questions=questions, memory=MemorySelection(campaign_id=None, limit=10)))
        bundle = compiled.bundle
        probe = self.adapter_factory(self.runtime)
        gate: AttemptGate = accountant.model_attempt_gate(
            task_id, max_output_tokens=self.max_output_tokens,
            input_tokens=bundle.estimated_tokens, task_limits=task_limits)
        request = ModelRequest.model_validate({
            "schema_version": 1, "request_id": request_id, "campaign_id": campaign_id,
            "task_id": task_id, "role": "explorer", "provider": probe.config.provider,
            "model": probe.config.model,
            "context_bundle": {"kind": "context_bundle", "id": bundle.bundle_id,
                               "sha256": compiled.sha256},
            "tools": [], "tool_results": [], "output_schema": bundle.output_schema.model_dump(),
            "continuation": None, "max_output_tokens": self.max_output_tokens,
            "timeout_ms": self.timeout_ms, "budget_reservation_id": gate.group_id,
            "stream": False})
        # A fresh runtime and adapter per call, so no gate is shared between calls.
        adapter = self.adapter_factory(dataclasses.replace(self.runtime, attempt_gate=gate))
        reply: ModelReply = adapter.complete(request, bundle, cancel=cancel)
        gate.settle(reply)
        proposal = None
        if reply.status == "completed" and reply.structured_output is not None:
            proposal = ExperimentProposal.model_validate(reply.structured_output)
        invocation = dataclasses.asdict(compiled.invocation)
        return ProposalOutcome(
            proposal.model_dump(mode="json") if proposal else None,
            reply.model_dump(mode="json"), invocation, compiled.sha256, len(gate.attempts),
            gate.exhausted.resource if gate.exhausted else None)
