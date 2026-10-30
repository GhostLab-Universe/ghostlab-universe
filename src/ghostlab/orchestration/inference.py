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

import httpx

from ghostlab.budgets import AttemptGate, BudgetAccountant
from ghostlab.context import ContextCompiler, ContextRequest, MemorySelection
from ghostlab.contracts import BudgetLimits, ExperimentProposal, ModelReply, ModelRequest
from ghostlab.providers import ProviderAdapter, ProviderConfig, Runtime

PASSING = {"passed"}
# Every check M01's probe (scripts/probe_provider.py) runs live; all must be present and pass.
REQUIRED_COMPATIBILITY_CHECKS = frozenset({
    "auth_minimal", "tool_roundtrip", "continuation_after_tool_result",
    "structured_proposal", "usage_capture"})


class LiveInferenceRefused(Exception):
    pass


@dataclass(frozen=True)
class LiveInferenceAuthorization:
    """Explicit operator authorization for real provider calls."""

    model: str
    max_model_calls: int          # spending cap, in HTTP attempts
    compatibility_report: Path    # M01 probe report from a live run
    confirm_paid_calls: bool

    def check(self, config: ProviderConfig, campaign_limits: BudgetLimits,
              schema: str = "experiment_proposal"):
        if not self.confirm_paid_calls:
            raise LiveInferenceRefused("paid calls were not confirmed")
        if self.model != config.model:
            raise LiveInferenceRefused("authorization names a different model")
        if not 1 <= self.max_model_calls <= campaign_limits.max_model_calls:
            raise LiveInferenceRefused("spending cap must be 1..campaign max_model_calls")
        try:
            report = json.loads(self.compatibility_report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise LiveInferenceRefused("compatibility report is missing or unreadable") from error
        if not isinstance(report, dict):
            raise LiveInferenceRefused("compatibility report is not a JSON object")
        if (report.get("live") is not True or report.get("transport") != "network"
                or report.get("mode") != "live"):
            raise LiveInferenceRefused("compatibility report is not from a live run")
        bound = {"provider": config.provider, "endpoint": config.base_url,
                 "model": config.model}
        for key, expected in bound.items():
            if report.get(key) != expected:
                raise LiveInferenceRefused(f"compatibility report is for a different {key}")
        checks = report.get("checks")
        if not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks):
            raise LiveInferenceRefused("compatibility report has no check list")
        if any(c.get("status") not in PASSING for c in checks):
            raise LiveInferenceRefused("compatibility report has failing or skipped checks")
        missing = REQUIRED_COMPATIBILITY_CHECKS - {c.get("check") for c in checks}
        if missing:
            raise LiveInferenceRefused(
                f"compatibility report lacks required checks: {sorted(missing)}")
        # M01's probe tests the refund proposal; another schema needs its own proof.
        proven = report.get("structured_schemas", ["experiment_proposal"])
        if not isinstance(proven, list) or schema not in proven:
            raise LiveInferenceRefused(f"compatibility report doesn't cover {schema}")


def require_inference_allowed(runtime: Runtime, config: ProviderConfig, limits: BudgetLimits,
                              live: LiveInferenceAuthorization | None,
                              *, schema: str = "experiment_proposal") -> BudgetLimits:
    """Return the effective limits; refuse any non-mock runtime without authorization.

    Only an ``httpx.MockTransport`` keeps requests in-process. ``None`` (the
    default network transport) or any real transport needs explicit authorization
    and a key read from the environment.
    """
    if isinstance(runtime.transport, httpx.MockTransport):
        return limits  # mocked transport: no request leaves the process
    if live is None:
        raise LiveInferenceRefused("a network runtime needs LiveInferenceAuthorization")
    if not config.credential.source.startswith("env:"):
        raise LiveInferenceRefused("live calls need a key read from the environment")
    live.check(config, limits, schema)
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
                task_limits: BudgetLimits, remaining_ms: int,
                deadline_passed: Callable[[], bool], cancel=None,
                skill_id: str = "propose-refund-scenario",
                world_version: str = "refund-world/1",
                proposal_model=ExperimentProposal) -> ProposalOutcome:
        compiled = self.compiler.compile(ContextRequest(
            bundle_id=f"bundle-{task_id}", campaign_id=campaign_id, task_id=task_id,
            role="explorer", scope=scope, objective=objective,
            success_criteria=success_criteria, skill_id=skill_id,
            as_of=as_of, compiled_at=as_of, token_budget=self.token_budget,
            questions=questions, memory=MemorySelection(
                world_version=world_version, campaign_id=None, limit=10)))
        bundle = compiled.bundle
        probe = self.adapter_factory(self.runtime)
        if remaining_ms < 1 or deadline_passed():
            return ProposalOutcome(None, {"status": "failed", "error": None}, {}, compiled.sha256,
                                   0, "wall_clock_ms")
        gate: AttemptGate = accountant.model_attempt_gate(
            task_id, max_output_tokens=self.max_output_tokens,
            input_tokens=bundle.estimated_tokens, task_limits=task_limits,
            deadline_passed=deadline_passed)
        request = ModelRequest.model_validate({
            "schema_version": 1, "request_id": request_id, "campaign_id": campaign_id,
            "task_id": task_id, "role": "explorer", "provider": probe.config.provider,
            "model": probe.config.model,
            "context_bundle": {"kind": "context_bundle", "id": bundle.bundle_id,
                               "sha256": compiled.sha256},
            "tools": [], "tool_results": [], "output_schema": bundle.output_schema.model_dump(),
            "continuation": None, "max_output_tokens": self.max_output_tokens,
            # The provider deadline never outlives the campaign's remaining wall clock.
            "timeout_ms": max(1, min(self.timeout_ms, remaining_ms)),
            "budget_reservation_id": gate.group_id,
            "stream": False})
        # A fresh runtime and adapter per call, so no gate is shared between calls.
        adapter = self.adapter_factory(dataclasses.replace(self.runtime, attempt_gate=gate))
        reply: ModelReply = adapter.complete(request, bundle, cancel=cancel)
        gate.settle(reply)
        proposal = None
        if reply.status == "completed" and reply.structured_output is not None:
            proposal = proposal_model.model_validate(reply.structured_output)
        invocation = dataclasses.asdict(compiled.invocation)
        return ProposalOutcome(
            proposal.model_dump(mode="json") if proposal else None,
            reply.model_dump(mode="json"), invocation, compiled.sha256, len(gate.attempts),
            gate.exhausted.resource if gate.exhausted else None)
