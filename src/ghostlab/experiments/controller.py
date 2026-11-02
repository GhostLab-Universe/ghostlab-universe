"""Controller plumbing: execute resolved configs through shared interfaces.

Execution drives the registered environment through the shared adapter
interface (``set_fault``/``reset``/``invoke``/``evidence``) and runs the shared
refund agent procedures with identical tool sequences and idempotency keys to
``ghostlab.core``. The controller owns ordering, budgets, evidence and
verdicts; it makes no inference calls (every run records ``model_calls: 0``).

Budgets survive retries and recovery: tool consumption is appended to the
project-scoped ledger keyed by resolved config, and every attempt — first try
or retry — checks the same envelope before touching the environment. Required
faults that never fired are recorded on the run, and a run is never a pass
when evidence is missing or the run did not complete.
"""

import time

from ..core import (BudgetExceeded, Fixture, PaymentTimeout, ToolError,
                    verify)
from ..envs import STATUS_QUALIFIED
from ..envs.adapters import (EnvCancelled, adapter_code_digest,
                             resolve_adapter_ref)
from ..envs.registry import utcnow
from .configs import EXPERIMENTS_SCHEMA_VERSION, ConfigError

RUN_STATUSES = ("completed", "budget_exhausted", "cancelled", "error")
VERDICTS = ("pass", "fail", "incomplete")
LEDGER_RESOURCE = "tool_calls"


class BudgetExhaustedError(Exception):
    """The aggregate envelope is spent; the environment was not touched."""


def fault_fired(hook: str, trace) -> bool:
    """Whether recorded evidence shows a fault hook fired.

    ``timeout_after_commit`` commits then reports a timeout, so it fired if
    and only if the trace carries a ``PaymentTimeout`` outcome. Unknown hooks
    never count as fired: they cannot reach execution (resolution rejects
    undiscovered hooks), so treating them as fired would hide a gap.
    """
    if type(trace) is not list:
        return False
    if hook == "timeout_after_commit":
        for event in trace:
            if not isinstance(event, dict):
                continue
            outcome = event.get("outcome")
            if isinstance(outcome, dict) \
                    and outcome.get("error") == "PaymentTimeout":
                return True
        return False
    return False


def evidence_complete(evidence, dispatches: int) -> bool:
    """Check controller-owned evidence is ordered, owned and consistent."""
    if type(evidence) is not dict or set(evidence) != {"snapshot", "trace"}:
        return False
    trace = evidence["trace"]
    snapshot = evidence["snapshot"]
    if type(trace) is not list or len(trace) != dispatches:
        return False
    if [e.get("event_id") for e in trace
            if isinstance(e, dict)] != list(range(1, len(trace) + 1)):
        return False
    if type(snapshot) is not dict or set(snapshot) != {
            "order", "ledger", "total_refunded_minor"}:
        return False
    ledger = snapshot["ledger"]
    if type(ledger) is not list:
        return False
    try:
        total = sum(row["amount_minor"] for row in ledger)
    except (KeyError, TypeError):
        return False
    return snapshot["total_refunded_minor"] == total


def evaluate_verdict(*, status: str, evidence_ok: bool, checks: dict,
                     rules, faults) -> str:
    """Derive pass/fail/incomplete from evidence, never from model text.

    Incomplete evidence or an unfinished run is ``incomplete``, never a pass.
    A completed run with complete evidence fails when any evaluated rule fails
    or any required fault never fired.
    """
    if status != "completed" or not evidence_ok:
        return "incomplete"
    if type(checks) is not dict:
        return "incomplete"
    for rule in rules:
        if checks.get(rule) is not True:
            return "fail"
    for fault in faults:
        if fault.get("required") is True and fault.get("fired") is not True:
            return "fail"
    return "pass"


def drive_agent(*, agent: str, invoke, identity: dict, paid_minor: int) -> None:
    """Shared refund agent procedure; identical sequence to ``core.run_agent``.

    ``invoke`` is any ``(tool, arguments) -> result`` dispatcher, so the same
    procedure drives a world or an environment adapter without divergence.
    """
    if agent not in ("vulnerable", "repaired"):
        raise ConfigError(f"unsupported agent {agent!r}")
    order = invoke("get_order", dict(identity))
    request = {**identity, "amount_minor": order["paid_minor"],
               "idempotency_key": "refund-intent-1"}
    try:
        invoke("issue_refund", request)
    except PaymentTimeout:
        if agent == "vulnerable":
            invoke("issue_refund", {**request,
                                    "idempotency_key": "refund-retry-2"})
        else:
            status = invoke("get_refund_status", {
                **identity, "idempotency_key": request["idempotency_key"]})
            if status["status"] != "committed":
                invoke("issue_refund", request)


def _new_adapter(registry, registration):
    cls = resolve_adapter_ref(registration["adapter_ref"],
                              registry.allowed_adapter_prefixes)
    return cls()


def execute_run(store, registry, *, project_id: str, resolved_id: str,
                repetition: int = 0, attempt: int = 0,
                cancel=None) -> dict:
    """Execute one repetition attempt and persist its run record.

    Refuses with :class:`BudgetExhaustedError` before touching the environment
    when the aggregate envelope is already spent, and with :class:`ConfigError`
    when the environment is not currently qualified for its declared
    capabilities or its adapter code changed since registration/resolution.
    Otherwise arms the resolved faults, resets the environment from the
    resolved fixture, drives the resolved agent within the per-run and
    aggregate budgets, then records evidence, checks, fault outcomes and the
    verdict. Cancellation is honored before every dispatch; a cancelled
    dispatch mutates nothing.
    """
    if type(repetition) is not int or repetition < 0:
        raise ConfigError("invalid repetition")
    if type(attempt) is not int or attempt < 0:
        raise ConfigError("invalid attempt")
    record = store.get_resolved(project_id, resolved_id)
    resolved = record["resolved"]
    effective = resolved["effective"]
    if repetition >= effective["repetitions"]["count"]:
        raise ConfigError(f"repetition {repetition} is outside the resolved"
                          f" count {effective['repetitions']['count']}")
    if store.attempt_recorded(resolved_id, repetition, attempt):
        raise ConfigError(f"attempt {attempt} of repetition {repetition} is"
                          " already recorded; retries use a new attempt number")
    limits = effective["limits"]
    aggregate = limits["max_total_tool_calls"]
    per_run = limits["max_tool_calls"]
    wall_budget = limits["max_wall_clock_ms"]
    used = store.budget_used(project_id, resolved_id, LEDGER_RESOURCE)
    if used >= aggregate:
        raise BudgetExhaustedError(
            f"budget envelope exhausted: {used}/{aggregate} tool calls"
            " consumed; the environment was not touched")
    seed = effective["seeds"][repetition]
    started_at = utcnow()
    started_mono = time.monotonic()
    try:
        registration = registry.get(resolved["pins"]["env_id"])
    except ValueError as error:
        raise ConfigError(f"execution env is no longer registered: {error}") \
            from error
    if registration["status"] != STATUS_QUALIFIED:
        raise ConfigError(
            f"env {resolved['pins']['env_id']!r} is not qualified for"
            f" execution (status {registration['status']!r}); qualify it"
            " before starting runs")
    adapter_cls = resolve_adapter_ref(registration["adapter_ref"],
                                      registry.allowed_adapter_prefixes)
    current_digest = adapter_code_digest(adapter_cls)
    if current_digest != registration["code_digest"] \
            or current_digest != resolved["pins"]["code_digest"]:
        raise ConfigError(
            "env adapter code changed since registration/resolution;"
            " re-register, re-qualify and re-resolve before starting runs")

    adapter = _new_adapter(registry, registration)
    try:
        supports_cancel = bool(adapter.describe().get(
            "supports_cancellation", False))
    except Exception as error:
        raise ConfigError(f"env describe() failed: {error}") from error
    for fault in effective["faults"]:
        try:
            adapter.set_fault(fault["hook"], dict(fault["params"]))
        except Exception as error:
            raise ConfigError(f"env rejected fault hook"
                              f" {fault['hook']!r}: {error}") from error
    try:
        adapter.reset(dict(effective["fixture"]))
    except Exception as error:
        raise ConfigError(f"env reset failed: {error}") from error

    dispatches = 0
    status = "completed"
    error_text = None

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    def wall_exceeded() -> bool:
        return (time.monotonic() - started_mono) * 1000 > wall_budget

    def invoke(tool: str, arguments: dict):
        nonlocal dispatches, status
        if cancelled():
            raise EnvCancelled("cancelled before dispatch")
        if wall_exceeded() or dispatches >= per_run \
                or used + dispatches >= aggregate:
            status = "budget_exhausted"
            raise BudgetExceeded("run budget exhausted")
        try:
            if cancel is not None and supports_cancel:
                result = adapter.invoke_cancellable(tool, arguments, cancel)
            else:
                if cancelled():
                    raise EnvCancelled("cancelled before dispatch")
                result = adapter.invoke(tool, arguments)
        except EnvCancelled:
            raise  # cancelled before dispatch: the world recorded nothing
        except BudgetExceeded:
            # The adapter world's own budget: raised pre-dispatch with no
            # trace event, so it is not counted as a dispatch.
            status = "budget_exhausted"
            raise
        except ToolError:
            # The world records rejected/error outcomes (including the
            # expected timeout-after-commit) as trace events and consumes
            # budget, so they count exactly like successes.
            dispatches += 1
            raise
        dispatches += 1
        return result

    if cancelled():
        status = "cancelled"
    else:
        identity = {"order_id": effective["fixture"]["order_id"],
                    "customer_id": effective["fixture"]["customer_id"]}
        try:
            drive_agent(agent=effective["agent"], invoke=invoke,
                        identity=identity,
                        paid_minor=effective["fixture"]["paid_minor"])
        except EnvCancelled:
            status = "cancelled"
        except BudgetExceeded:
            status = "budget_exhausted"
        except PaymentTimeout as error:  # pragma: no cover - driver handles it
            status = "error"
            error_text = f"{type(error).__name__}: {error}"
        except ToolError as error:
            status = "error"
            error_text = f"{type(error).__name__}: {error}"
    completed_at = utcnow()
    wall_ms = int((time.monotonic() - started_mono) * 1000)

    try:
        evidence = adapter.evidence()
    except Exception as error:
        evidence = {"snapshot": None, "trace": None}
        if status == "completed":
            status = "error"
            error_text = f"evidence unavailable: {type(error).__name__}"
    ok = evidence_complete(evidence, dispatches)
    checks: dict = {}
    if isinstance(evidence.get("snapshot"), dict):
        try:
            checks = verify(Fixture(**effective["fixture"]),
                            evidence["snapshot"])
        except (ValueError, KeyError, TypeError):
            checks = {}
            ok = False
    else:
        ok = False
    faults = [{"hook": f["hook"], "required": f["required"],
               "fired": fault_fired(f["hook"], evidence.get("trace"))}
              for f in effective["faults"]]
    verdict = evaluate_verdict(status=status, evidence_ok=ok, checks=checks,
                               rules=effective["rules"], faults=faults)
    body = {"schema_version": EXPERIMENTS_SCHEMA_VERSION,
            "project_id": project_id, "resolved_id": resolved_id,
            "repetition": repetition, "attempt": attempt, "seed": seed,
            "status": status, "verdict": verdict, "model_calls": 0,
            "tool_calls": dispatches, "wall_clock_ms": wall_ms,
            "faults": faults, "evidence": evidence, "checks": checks,
            "rules_evaluated": list(effective["rules"]),
            "rules_failed": [r for r in effective["rules"]
                             if checks.get(r) is not True],
            "error": error_text, "started_at": started_at,
            "completed_at": completed_at}
    stored = store.save_run(project_id=project_id, resolved_id=resolved_id,
                            repetition=repetition, attempt=attempt,
                            body=body, created_at=completed_at)
    store.add_ledger(project_id=project_id, resolved_id=resolved_id,
                     run_id=stored["run_id"], resource=LEDGER_RESOURCE,
                     amount=dispatches, created_at=completed_at)
    return stored
