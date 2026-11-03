"""Controller plumbing: execute resolved configs through shared interfaces.

Execution drives the registered environment through the shared adapter
interface (``set_fault``/``reset``/``invoke``/``evidence``) and dispatches
the agent procedure, evidence shape, fault reading and rule verification to
the resolved environment's driver (refunds keep identical tool sequences and
idempotency keys to ``ghostlab.core``). The controller owns ordering,
budgets, evidence and verdicts; it makes no inference calls (every run
records ``model_calls: 0``).

Budgets survive retries and recovery: tool consumption is appended to the
project-scoped ledger keyed by resolved config, and every attempt — first try
or retry — checks the same envelope before touching the environment. Required
faults that never fired are recorded on the run, and a run is never a pass
when evidence is missing or the run did not complete.

Occurrence triggers: faults without ``trigger`` are armed at reset and fire
on the first opportunity, exactly as before. Faults with ``trigger`` are NOT
armed at reset; instead, before each dispatch the controller counts the
upcoming dispatch (per-tool count for a named tool, total dispatches for
``"*"``) and arms each still-unarmed fault whose occurrence matches, then
disarms right after that dispatch so it fires at most once. Triggered faults
are recorded with their ``trigger`` and ``fired_occurrence`` (the counted
dispatch that armed them when the trace shows they fired, else null);
untriggered faults keep the exact ``{"hook", "required", "fired"}`` record.
Late arming needs adapter support: ``set_fault`` must take effect between
``reset`` and ``invoke``. Verdict semantics are unchanged: a required fault
that never fired fails the run as untested, never passes it.
"""

import time

from ..core import BudgetExceeded, PaymentTimeout, ToolError
from ..envs import STATUS_QUALIFIED
from ..envs.adapters import (EnvCancelled, RefundEnvAdapter,
                             adapter_code_digest, resolve_adapter_ref)
from ..envs.registry import utcnow
from . import drivers as _drivers
from . import drivers_refund as _refund_driver
from .configs import EXPERIMENTS_SCHEMA_VERSION, ConfigError

RUN_STATUSES = ("completed", "budget_exhausted", "cancelled", "error")
VERDICTS = ("pass", "fail", "incomplete")
LEDGER_RESOURCE = "tool_calls"


class BudgetExhaustedError(Exception):
    """The aggregate envelope is spent; the environment was not touched."""


def fault_fired(hook: str, trace) -> bool:
    """Whether recorded evidence shows a fault hook fired.

    Thin refund delegate over the refund driver; kept for existing tests.
    Execution dispatches to the resolved environment's driver instead.
    """
    return _refund_driver.fault_fired(hook, trace)


def evidence_complete(evidence, dispatches: int) -> bool:
    """Check controller-owned evidence is ordered, owned and consistent.

    Thin refund delegate over the refund driver; kept for existing tests.
    Execution dispatches to the resolved environment's driver instead.
    """
    return _refund_driver.evidence_complete(evidence, dispatches)


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

    Thin refund delegate over the refund driver; kept for existing tests.
    ``invoke`` is any ``(tool, arguments) -> result`` dispatcher, so the same
    procedure drives a world or an environment adapter without divergence.
    """
    if agent not in ("vulnerable", "repaired"):
        raise ConfigError(f"unsupported agent {agent!r}")
    fixture = {"order_id": identity["order_id"],
               "customer_id": identity["customer_id"],
               "paid_minor": paid_minor, "currency": "INR"}
    _refund_driver.drive(agent, invoke, fixture)


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
    env_id = resolved["pins"]["env_id"]
    driver = _drivers.get_driver(env_id)
    if driver is None and isinstance(adapter, RefundEnvAdapter):
        # Backward compatibility: refund adapters registered under any
        # env id run the refund driver (explicit registration wins).
        driver = _refund_driver
    if driver is None:
        raise ConfigError(
            f"no driver registered for env {env_id!r}; register the"
            " environment's driver before starting runs")
    if effective["agent"] not in driver.agents():
        raise ConfigError(f"unsupported agent {effective['agent']!r}"
                          f" for env {env_id!r}")
    untriggered = [f for f in effective["faults"] if "trigger" not in f]
    pending = [{"spec": f, "armed": False, "armed_occurrence": None}
               for f in effective["faults"] if "trigger" in f]
    for fault in untriggered:
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
    tool_counts: dict = {}

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
        upcoming_tool_count = tool_counts.get(tool, 0) + 1
        upcoming_total = dispatches + 1
        armed_now = []
        for entry in pending:
            if entry["armed"]:
                continue
            trigger = entry["spec"]["trigger"]
            if trigger["tool"] == "*":
                matched = upcoming_total == trigger["occurrence"]
                counted = upcoming_total
            else:
                matched = tool == trigger["tool"] \
                    and upcoming_tool_count == trigger["occurrence"]
                counted = upcoming_tool_count
            if not matched:
                continue
            try:
                adapter.set_fault(entry["spec"]["hook"],
                                  dict(entry["spec"]["params"]))
            except Exception as error:
                raise ConfigError(
                    f"env rejected fault hook"
                    f" {entry['spec']['hook']!r}: {error}") from error
            entry["armed"] = True
            entry["armed_occurrence"] = counted
            armed_now.append(entry)
        tool_counts[tool] = upcoming_tool_count
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
        except Exception:
            # Non-core environment errors (e.g. an example world's own
            # timeout type, which the driver handles): counted exactly
            # like core ToolError outcomes. Core BudgetExceeded and
            # cancellation are handled above and never reach here.
            dispatches += 1
            raise
        else:
            dispatches += 1
            return result
        finally:
            if armed_now:
                # Fire-at-most-once: disarm right after the counted dispatch
                # so the hook cannot leak onto a later one, then restore the
                # reset-time arming of untriggered faults (clear_faults is
                # adapter-wide). A disarm failure aborts like any other env
                # arming failure: no run record is written.
                try:
                    adapter.clear_faults()
                    for fault in untriggered:
                        adapter.set_fault(fault["hook"],
                                          dict(fault["params"]))
                except Exception as error:
                    raise ConfigError(
                        "env failed to disarm a triggered fault"
                        f" after dispatch: {error}") from error

    if cancelled():
        status = "cancelled"
    else:
        try:
            driver.drive(effective["agent"], invoke,
                         dict(effective["fixture"]))
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
        except Exception as error:
            # A non-core environment error the driver did not handle:
            # recorded as an error run, like an unhandled core ToolError.
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
    ok = driver.evidence_complete(evidence, dispatches)
    checks: dict = {}
    if isinstance(evidence.get("snapshot"), dict):
        try:
            checks = driver.verify_checks(dict(effective["fixture"]),
                                          evidence["snapshot"])
        except (ValueError, KeyError, TypeError):
            checks = {}
            ok = False
    else:
        ok = False
    armed_by_hook = {e["spec"]["hook"]: e["armed_occurrence"]
                     for e in pending}
    faults = []
    for f in effective["faults"]:
        entry = {"hook": f["hook"], "required": f["required"],
                 "fired": driver.fault_fired(f["hook"],
                                             evidence.get("trace"))}
        if "trigger" in f:
            armed_occurrence = armed_by_hook.get(f["hook"])
            entry["fired_occurrence"] = armed_occurrence \
                if entry["fired"] else None
            entry["trigger"] = dict(f["trigger"])
        faults.append(entry)
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
