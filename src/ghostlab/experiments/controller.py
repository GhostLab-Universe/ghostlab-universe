"""Controller plumbing: execute resolved configs through shared interfaces.

Execution drives the registered environment through the shared adapter
interface (``set_fault``/``reset``/``invoke``/``evidence``) and dispatches
the agent procedure, evidence shape, fault reading and rule verification to
the resolved environment's driver (refunds keep identical tool sequences and
idempotency keys to ``ghostlab.core``). The controller owns ordering,
budgets, evidence and verdicts; it makes no inference calls (every run
records ``model_calls: 0``).

Backend runs: a resolved config pinned to a backend other than
``trusted-local`` executes as one controller-assigned job on that
``ExecutionBackend`` instead of in-process. The controller never imports
adapter code on this path: it sends ``hello``/``set_fault``/``reset``/
``invoke``/``evidence``/``close`` commands, validates every event
envelope, rehydrates tool errors by name (core map, then the trusted
driver module's own error classes, else a generic environment error),
and records a project-scoped receipt with observed-vs-claimed
separation. Unknown or unavailable backends are refused before any job
starts (no run record, never a silent fallback); once a job has
started, every outcome is recorded honestly (completed, budget
exhausted, cancelled or error) with termination, truncation and
missing-evidence markers preserved.

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

import hashlib
import json
import time
import uuid

from ..core import BudgetExceeded, PaymentTimeout, ToolError
from ..envs import STATUS_QUALIFIED
from ..envs.adapters import (EnvCancelled, EnvError, RefundEnvAdapter,
                             adapter_code_digest, resolve_adapter_ref)
from ..envs.registry import utcnow
from . import drivers as _drivers
from . import drivers_refund as _refund_driver
from .configs import (BACKEND_NAME_PATTERN, EXPERIMENTS_SCHEMA_VERSION,
                      TRUSTED_LOCAL_BACKEND, ConfigError,
                      validate_backend_pin)

RUN_STATUSES = ("completed", "budget_exhausted", "cancelled", "error")
VERDICTS = ("pass", "fail", "incomplete")
LEDGER_RESOURCE = "tool_calls"

# Backend-run tuning: poll slices bound cancel/wall latency, cancel grace
# bounds teardown, and the close poll bounds the final event drain.
POLL_SLICE_MS = 500
CANCEL_GRACE_MS = 5000
CLOSE_POLL_MS = 10000


class BudgetExhaustedError(Exception):
    """The aggregate envelope is spent; the environment was not touched."""


_BACKENDS: dict = {}
_BACKEND_METHODS = ("prepare", "start_job", "send", "poll", "status",
                    "cancel", "close", "capabilities")


def register_backend(name: str, backend) -> None:
    """Register one execution backend instance under ``name`` (no redefine).

    ``trusted-local`` is reserved for the in-process path and cannot be
    registered. The instance must expose the frozen ``ExecutionBackend``
    surface; anything else is rejected here, fail fast.
    """
    if type(name) is not str or not BACKEND_NAME_PATTERN.match(name):
        raise ValueError(f"invalid backend name {name!r}")
    if name == TRUSTED_LOCAL_BACKEND:
        raise ValueError("cannot register reserved backend"
                         f" {TRUSTED_LOCAL_BACKEND!r}")
    missing = [method for method in _BACKEND_METHODS
               if not callable(getattr(backend, method, None))]
    if missing:
        raise ValueError(f"backend {name!r} misses: {', '.join(missing)}")
    if name in _BACKENDS:
        raise ValueError(f"backend {name!r} is already registered;"
                         " backends are never redefined")
    _BACKENDS[name] = backend


def unregister_backend(name: str) -> None:
    """Remove one backend registration (operator disqualification)."""
    if name not in _BACKENDS:
        raise ValueError(f"unknown backend {name!r}")
    del _BACKENDS[name]


def get_backend(name):
    """Return the registered backend, or refuse unknown names."""
    if type(name) is not str or name not in _BACKENDS:
        raise ConfigError(
            f"unknown backend {name!r}; register the backend before"
            " resolving or starting runs")
    return _BACKENDS[name]


def registered_backends() -> dict:
    """Return a copy of the backend registry (name to instance)."""
    return dict(_BACKENDS)


def _check_backend_surface(name: str, backend) -> None:
    """Refuse backend instances that lack the frozen surface."""
    missing = [method for method in _BACKEND_METHODS
               if not callable(getattr(backend, method, None))]
    if missing:
        raise ConfigError(
            f"backend {name!r} misses: {', '.join(missing)};"
            " refusing to execute")


class _JobFailed(Exception):
    """The backend job failed or violated the protocol (error run)."""

    def __init__(self, message: str, kind: str = "backend_failed"):
        super().__init__(message)
        self.kind = kind


class _JobTruncated(Exception):
    """Job output was truncated (budget-exhausted run, markers preserved)."""


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


def _backend_pin_for_run(resolved: dict) -> dict:
    """Return the validated backend pin, defaulting legacy resolves.

    Resolved configs written before backend pinning carry no pin and
    execute as ``trusted-local``. Malformed pins are refused.
    """
    pin = resolved["pins"].get("backend")
    if pin is None:
        return {"name": TRUSTED_LOCAL_BACKEND,
                "backend_version": "trusted-local/1"}
    return validate_backend_pin(pin)


def execute_run(store, registry, *, project_id: str, resolved_id: str,
                repetition: int = 0, attempt: int = 0,
                cancel=None, backends=None,
                receipt_store=None) -> dict:
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

    When the resolved config pins a backend other than ``trusted-local``,
    the run executes as one job on that backend (see the module docstring):
    unknown or unavailable backends are refused with no run record and
    never a silent fallback. ``backends`` overrides the backend registry
    for this call (a name-to-instance mapping); ``receipt_store`` overrides
    the derived job-receipt file. Both default to the shared wiring.
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

    backend_pin = _backend_pin_for_run(resolved)
    if backend_pin["name"] != TRUSTED_LOCAL_BACKEND:
        return _execute_backend_run(
            store, project_id=project_id, resolved_id=resolved_id,
            repetition=repetition, attempt=attempt, resolved=resolved,
            seed=seed, started_at=started_at, started_mono=started_mono,
            used=used, backend_pin=backend_pin, cancel=cancel,
            backends=backends, receipt_store=receipt_store)

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

    model_calls = 0
    if cancelled():
        status = "cancelled"
    else:
        try:
            driven = driver.drive(effective["agent"], invoke,
                                  dict(effective["fixture"]))
            if isinstance(driven, dict):
                model_calls = int(driven.get("model_calls", 0))
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
            "status": status, "verdict": verdict, "model_calls": model_calls,
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


def _scrub_facts(node, sanitize, depth: int = 0):
    """Copy backend-claimed facts with every string sanitized."""
    if depth > 8:
        return "<deep>"
    if isinstance(node, dict):
        return {key if type(key) is str else "<invalid-key>":
                _scrub_facts(value, sanitize, depth + 1)
                for key, value in node.items()}
    if isinstance(node, list):
        return [_scrub_facts(value, sanitize, depth + 1) for value in node]
    if isinstance(node, str):
        return sanitize(node, 1000)
    if node is None or isinstance(node, (bool, int, float)):
        return node
    return f"<invalid:{type(node).__name__}>"


# Error names a backend must never rehydrate: they steer controller
# control flow (cancel/budget) or would mislabel a tool failure as a
# controller-level refusal. Anything blocked here becomes a generic
# environment error instead (recorded as an error run).
_REHYDRATE_BLOCKED = frozenset({
    "BudgetExhausted", "BudgetExhaustedError", "ConfigError",
    "CrossProjectError", "EnvCancelled"})


def _rehydrate_tool_error(driver, name, message, sanitize):
    """Rebuild a tool error from its backend-reported name.

    Core error names map to the core classes; anything else resolves
    against the trusted driver module's own namespace (which is how the
    booking driver's ``BookingTimeout`` — a member of the booking
    world's own hierarchy — is found without importing any adapter
    code). Candidates must be plain exception classes outside the
    control-flow blocklist, so a hostile name can at worst produce an
    error run, never a forged cancellation, budget state or refusal.
    Unknown names become a generic environment error, which runs record
    as errors.
    """
    core = {"ToolError": ToolError, "PaymentTimeout": PaymentTimeout}
    cls = core.get(name) if type(name) is str else None
    if cls is None and type(name) is str and name.isidentifier() \
            and name not in _REHYDRATE_BLOCKED:
        candidate = getattr(driver, name, None)
        if isinstance(candidate, type) \
                and issubclass(candidate, Exception):
            cls = candidate
    if cls is None:
        return EnvError(f"backend tool error {sanitize(name, 100)!r}:"
                        f" {sanitize(message)}")
    try:
        return cls(sanitize(message))
    except Exception:
        return EnvError(f"backend tool error {sanitize(name, 100)!r}:"
                        f" {sanitize(message)}")


def _check_backend_effective(effective) -> None:
    """Refuse malformed resolved effective values before starting a job."""
    if type(effective) is not dict:
        raise ConfigError("invalid resolved effective values")
    for key in ("agent", "fixture", "faults", "rules", "limits",
                "repetitions", "seeds"):
        if key not in effective:
            raise ConfigError(f"resolved effective values lack {key!r}")
    if type(effective["agent"]) is not str \
            or type(effective["fixture"]) is not dict \
            or type(effective["faults"]) is not list \
            or type(effective["rules"]) is not list \
            or type(effective["limits"]) is not dict \
            or type(effective["seeds"]) is not list:
        raise ConfigError("invalid resolved effective values")
    for key in ("max_tool_calls", "max_total_tool_calls",
                "max_wall_clock_ms"):
        if type(effective["limits"].get(key)) is not int:
            raise ConfigError(f"invalid resolved limits.{key}")
    for index, fault in enumerate(effective["faults"]):
        if type(fault) is not dict or type(fault.get("hook")) is not str \
                or type(fault.get("required")) is not bool \
                or type(fault.get("params")) is not dict:
            raise ConfigError(f"invalid resolved faults[{index}]")
        trigger = fault.get("trigger")
        if trigger is not None and (
                type(trigger) is not dict
                or type(trigger.get("tool")) is not str
                or type(trigger.get("occurrence")) is not int):
            raise ConfigError(f"invalid resolved faults[{index}].trigger")


def _execute_backend_run(store, *, project_id: str, resolved_id: str,
                         repetition: int, attempt: int, resolved: dict,
                         seed: int, started_at: str, started_mono: float,
                         used: int, backend_pin: dict, cancel,
                         backends, receipt_store) -> dict:
    """Execute one repetition attempt as a job on an execution backend.

    Refusals (unknown backend, broken surface, malformed pin, unavailable
    backend, package mismatch) raise before any run record exists and
    never fall back to another backend. Once ``start_job`` succeeds,
    every outcome is recorded: completed, budget-exhausted, cancelled or
    error, with termination, truncation and missing-evidence preserved.
    """
    # Function-level import: the receipts module reuses the experiment
    # store's project error, so a top-level import would cycle back
    # through this package's __init__ into this module.
    from ..backends.receipts import (BackendError, BackendUnavailableError,
                                     JobHandle, JobReceiptStore, JobSpec,
                                     MAX_JOB_OUTPUT_BYTES, ProtocolError,
                                     sanitize_untrusted_text)
    sanitize = sanitize_untrusted_text
    backend_name = backend_pin["name"]
    effective = resolved["effective"]
    _check_backend_effective(effective)
    limits = effective["limits"]
    aggregate = limits["max_total_tool_calls"]
    per_run = limits["max_tool_calls"]
    wall_budget = limits["max_wall_clock_ms"]
    if backends is None:
        backend = get_backend(backend_name)
    else:
        try:
            backend = backends[backend_name]
        except (KeyError, TypeError):
            raise ConfigError(
                f"unknown backend {backend_name!r}; register the backend"
                " before resolving or starting runs") from None
    _check_backend_surface(backend_name, backend)
    env_id = resolved["pins"]["env_id"]
    driver = _drivers.get_driver(env_id)
    if driver is None:
        raise ConfigError(
            f"no driver registered for env {env_id!r}; backend runs"
            " require an explicitly registered driver (the controller"
            " never imports adapter code on this path)")
    if effective["agent"] not in driver.agents():
        raise ConfigError(f"unsupported agent {effective['agent']!r}"
                          f" for env {env_id!r}")
    package_kind = backend_pin.get("package_kind")
    package_identity = backend_pin.get("package_identity")
    if not package_kind or not package_identity:
        raise ConfigError(
            f"backend {backend_name!r} pin lacks the package pair;"
            " re-resolve before starting runs")
    package = {"kind": package_kind, "identity": package_identity}
    if receipt_store is None:
        sibling = store.db_path.with_name(
            store.db_path.stem + "-job-receipts" + store.db_path.suffix)
        receipt_store = JobReceiptStore(sibling)
    job_id = "job-" + uuid.uuid4().hex[:16]
    try:
        receipt_store.create_job(
            project_id=project_id, job_id=job_id, backend=backend_name,
            backend_version=backend_pin["backend_version"],
            package=dict(package))
    except ValueError as error:
        raise ConfigError(f"cannot open job receipt: {error}") from error

    def fail_receipt(note: str) -> None:
        try:
            receipt_store.record_state(project_id, job_id, "failed",
                                       observed=True, note=note[:500])
        except Exception:
            pass

    def observe(facts: dict) -> None:
        receipt_store.update_observed(project_id, job_id, facts)

    def claim(facts: dict) -> None:
        receipt_store.update_claimed(project_id, job_id, facts)

    try:
        prepared = backend.prepare(dict(package))
    except BackendUnavailableError:
        fail_receipt("prepare unavailable")
        raise
    except BackendError:
        fail_receipt("prepare failed")
        raise
    except Exception as error:
        fail_receipt("prepare crashed")
        raise BackendError(
            f"backend {backend_name!r} prepare failed:"
            f" {sanitize(str(error))}") from error
    if type(prepared) is not dict \
            or set(prepared) != {"identity", "provenance"}:
        fail_receipt("prepare returned a malformed pin")
        raise ProtocolError(
            f"backend {backend_name!r} prepare must return"
            " {identity, provenance}")
    if prepared["identity"] != package_identity:
        fail_receipt("prepare pinned a different identity")
        raise ConfigError(
            f"backend {backend_name!r} pinned package identity"
            f" {sanitize(prepared['identity'], 100)!r}, which does not"
            " match the resolved pin; refusing to execute")
    if type(prepared["provenance"]) is not dict:
        fail_receipt("prepare returned malformed provenance")
        raise ProtocolError(
            f"backend {backend_name!r} prepare provenance must be"
            " an object")
    try:
        claim({"prepare": _scrub_facts(prepared["provenance"], sanitize)})
    except (ProtocolError, ValueError) as error:
        fail_receipt("prepare provenance exceeds bounds")
        raise ProtocolError(
            f"backend {backend_name!r} prepare provenance invalid:"
            f" {error}") from error
    try:
        spec = JobSpec(job_id=job_id, project_id=project_id,
                       package=dict(package),
                       adapter_ref=resolved["pins"]["adapter_ref"],
                       fixture=dict(effective["fixture"]),
                       limits={"wall_clock_ms": wall_budget,
                               "max_output_bytes": MAX_JOB_OUTPUT_BYTES},
                       no_network=True)
    except (ValueError, TypeError) as error:
        fail_receipt("job spec invalid")
        raise ConfigError(f"cannot build job spec: {error}") from error
    handle = JobHandle(receipt_store, backend, project_id, job_id)
    try:
        start_ack = backend.start_job(spec)
    except BackendUnavailableError:
        fail_receipt("start unavailable")
        raise
    except BackendError:
        fail_receipt("start failed")
        raise
    except Exception as error:
        fail_receipt("start crashed")
        raise BackendError(
            f"backend {backend_name!r} start_job failed:"
            f" {sanitize(str(error))}") from error
    if start_ack is not None:
        try:
            handle.cancel(0)
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass
        try:
            receipt_store.mark_unresolved(
                project_id, job_id,
                "start_job returned a value (must return None);"
                " abandon attempted, no command was sent")
        except Exception:
            pass
        raise ProtocolError(
            f"backend {backend_name!r} start_job must return None")
    try:
        receipt_store.record_state(project_id, job_id, "starting",
                                   observed=True,
                                   note=f"job started on {backend_name}")
    except Exception as error:
        try:
            handle.cancel(0)
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass
        raise BackendError(
            "receipt store failure after start; abandon attempted, no"
            f" command was sent: {sanitize(str(error))}") from error

    dispatches = 0
    invoked_any = False
    status = "completed"
    error_text = None
    truncated = False
    missing_evidence = False
    job_state = "starting"
    termination = None
    cmd_seq = 0
    event_seq = 0
    output_bytes = 0
    tool_counts: dict = {}
    untriggered = [f for f in effective["faults"] if "trigger" not in f]
    pending = [{"spec": f, "armed": False, "armed_occurrence": None}
               for f in effective["faults"] if "trigger" in f]

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    def wall_exceeded() -> bool:
        return (time.monotonic() - started_mono) * 1000 > wall_budget

    def remaining_ms() -> int:
        left = wall_budget - int((time.monotonic() - started_mono) * 1000)
        return left if left > 0 else 0

    def send_command(cmd_type: str, payload=None) -> None:
        nonlocal cmd_seq, saw_backend_timeout
        command = {"type": cmd_type, "payload": dict(payload or {})}
        try:
            receipt_store.record_command(project_id, job_id, command,
                                         cmd_seq)
        except (ProtocolError, ValueError) as error:
            raise _JobFailed(f"protocol violation: cannot record command"
                             f" {cmd_type!r}: {error}",
                             kind="protocol_violation") from error
        except Exception as error:
            raise _JobFailed("receipt store failure:"
                             f" {sanitize(str(error))}") from error
        cmd_seq += 1
        try:
            handle.send(command)
        except BackendError as error:
            detail = sanitize(str(error))
            try:
                hint = handle.status()
            except Exception:
                hint = None
            state = hint.get("state") if type(hint) is dict else None
            if state == "timed_out":
                saw_backend_timeout = True
                try:
                    observe({"backend_timed_out": True})
                except Exception:
                    pass
                raise BudgetExceeded(
                    "backend reported timed out") from error
            if state in ("completed", "failed", "cancelled"):
                raise _JobFailed(
                    f"backend job is {state}: {detail}") from error
            raise _JobFailed(
                f"backend send failed: {detail}") from error
        except Exception as error:
            raise _JobFailed(f"backend send crashed:"
                             f" {sanitize(str(error))}") from error

    def poll_events(timeout_ms: int) -> list:
        nonlocal event_seq, output_bytes
        try:
            events = handle.poll(timeout_ms)
        except BackendError as error:
            raise _JobFailed(
                f"backend poll failed: {sanitize(str(error))}") from error
        except Exception as error:
            raise _JobFailed(
                f"backend poll crashed:"
                f" {sanitize(str(error))}") from error
        if type(events) is not list:
            raise _JobFailed("protocol violation: backend poll returned"
                             " a non-list",
                             kind="protocol_violation")
        for event in events:
            try:
                receipt_store.record_event(project_id, job_id, event,
                                           expected_seq=event_seq)
            except BackendError as error:
                raise _JobFailed(
                    "protocol violation: malformed backend event:"
                    f" {sanitize(str(error))}",
                    kind="protocol_violation") from error
            except Exception as error:
                raise _JobFailed(
                    f"receipt store failure:"
                    f" {sanitize(str(error))}") from error
            try:
                raw = json.dumps(event, sort_keys=True,
                                 separators=(",", ":"),
                                 ensure_ascii=True).encode("utf-8")
            except (TypeError, ValueError):
                raise _JobFailed(
                    "protocol violation: backend event is not strict"
                    " JSON",
                    kind="protocol_violation")
            output_bytes += len(raw)
            if output_bytes > MAX_JOB_OUTPUT_BYTES:
                raise _JobTruncated(
                    "controller output cap exceeded"
                    f" ({output_bytes}/{MAX_JOB_OUTPUT_BYTES} bytes)")
            event_seq += 1
        return events

    def failed_detail(event: dict):
        payload = event.get("payload", {})
        name = sanitize(payload.get("error", "BackendFailed"), 100)
        message = sanitize(payload.get("message", ""), 300)
        return (f"backend job failed: {name}: {message}".rstrip(": "),
                payload.get("truncated") is True
                or payload.get("error") == "output-exhausted")

    saw_backend_timeout = False

    def await_types(expect: set, *, what: str, budget_ms=None):
        nonlocal saw_backend_timeout
        deadline = None if budget_ms is None else time.monotonic() \
            + budget_ms / 1000.0
        while True:
            # Arrived events are processed first: like the in-process
            # path (an in-flight call completes normally), a result
            # already delivered is consumed before cancel/wall stop
            # the wait. The next dispatch pre-check then honors them.
            events = poll_events(
                min(POLL_SLICE_MS, max(1, remaining_ms())))
            for event in events:
                etype = event["type"]
                if etype == "failed":
                    detail, was_truncated = failed_detail(event)
                    if was_truncated:
                        raise _JobTruncated(detail)
                    raise _JobFailed(detail)
                if etype == "truncated":
                    payload = event.get("payload", {})
                    raise _JobTruncated(
                        "backend reported truncation:"
                        f" {sanitize(payload.get('reason', ''), 200)}"
                        .rstrip(": "))
                if etype in expect:
                    return event
                raise _JobFailed(
                    f"protocol violation: unexpected {etype!r} while"
                    f" awaiting {what}",
                    kind="protocol_violation")
            # No events arrived: honor stop conditions, then peek at the
            # backend's claimed state so a backend-side deadline maps
            # promptly instead of idling. A failed peek is ignored.
            if cancelled():
                raise EnvCancelled(f"cancelled while awaiting {what}")
            if wall_exceeded():
                raise BudgetExceeded(
                    f"wall clock exhausted while awaiting {what}")
            if deadline is not None and time.monotonic() >= deadline:
                raise _JobFailed(f"timed out awaiting {what}")
            try:
                hint = handle.status()
            except Exception:
                hint = None
            if type(hint) is dict and hint.get("state") in (
                    "completed", "failed", "cancelled", "timed_out"):
                claimed = hint["state"]
                if claimed == "timed_out":
                    saw_backend_timeout = True
                    try:
                        observe({"backend_timed_out": True})
                    except Exception:
                        pass
                    raise BudgetExceeded("backend reported timed out")
                raise _JobFailed(
                    f"backend entered unexpected state {claimed!r}"
                    f" while awaiting {what}")

    def send_and_ack(cmd_type: str, payload, what: str) -> None:
        send_command(cmd_type, payload)
        event = await_types({"tool_result"}, what=what)
        result = event.get("payload", {})
        if result.get("ok") is True:
            return
        name = sanitize(result.get("error", "BackendFailed"), 100)
        message = sanitize(result.get("message", ""), 300)
        raise _JobFailed(
            f"{what} rejected by backend: {name}: {message}".rstrip(": "))

    def invoke(tool: str, arguments: dict):
        nonlocal dispatches, invoked_any, status
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
            send_and_ack("set_fault",
                         {"name": entry["spec"]["hook"], "params": {}},
                         f"trigger arm {entry['spec']['hook']}")
            entry["armed"] = True
            entry["armed_occurrence"] = counted
            armed_now.append(entry)
        tool_counts[tool] = upcoming_tool_count
        try:
            send_command("invoke", {"tool": tool,
                                    "arguments": dict(arguments)
                                    if type(arguments) is dict
                                    else arguments})
            invoked_any = True
            event = await_types({"tool_result"}, what=f"tool {tool}")
        finally:
            if armed_now:
                # Fire-at-most-once, mirroring the in-process path: disarm
                # right after the counted dispatch, then restore the
                # reset-time arming. A disarm failure aborts the drive
                # and is recorded as an error run (post-start honesty).
                send_and_ack("clear_faults", {}, "trigger disarm")
                for fault in untriggered:
                    send_and_ack(
                        "set_fault",
                        {"name": fault["hook"],
                         "params": dict(fault["params"])},
                        f"re-arm {fault['hook']}")
        result = event.get("payload", {})
        if result.get("ok") is True:
            dispatches += 1
            return result.get("result")
        name = result.get("error", "AdapterError")
        message = result.get("message", "")
        if name == "BudgetExceeded":
            status = "budget_exhausted"
            raise BudgetExceeded("backend tool budget exhausted")
        dispatches += 1
        raise _rehydrate_tool_error(driver, name, message, sanitize)

    def collect_evidence():
        send_command("evidence", {})
        event = await_types({"evidence_chunk"}, what="evidence")
        payload = event.get("payload", {})
        if type(payload.get("evidence")) is not dict:
            raise _JobFailed(
                "protocol violation: evidence_chunk lacks an evidence"
                " object",
                kind="protocol_violation")
        raw_evidence = payload["evidence"]
        try:
            raw = json.dumps(raw_evidence, sort_keys=True,
                             separators=(",", ":"),
                             ensure_ascii=True,
                             allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise _JobFailed(
                "protocol violation: evidence is not strict JSON",
                kind="protocol_violation")
        observed_digest = hashlib.sha256(raw).hexdigest()
        try:
            observe({"evidence_digest": observed_digest,
                     "evidence_bytes": len(raw)})
        except Exception:
            pass
        claimed = payload.get("evidence_digest")
        if claimed is not None:
            if type(claimed) is not str:
                raise _JobFailed(
                    "protocol violation: claimed evidence digest is not"
                    " a string",
                    kind="protocol_violation")
            try:
                claim({"evidence_digest": sanitize(claimed, 128)})
            except Exception:
                pass
            if claimed != observed_digest:
                try:
                    observe({"evidence_mismatch": True})
                except Exception:
                    pass
                raise _JobFailed(
                    "backend evidence digest mismatch"
                    " (observed-vs-claimed)")
        return raw_evidence

    def cancel_job(reason: str) -> None:
        nonlocal job_state, termination
        try:
            ack = handle.cancel(CANCEL_GRACE_MS)
        except Exception as error:
            try:
                observe({"cancel_failed": sanitize(str(error))})
            except Exception:
                pass
            termination = {"reason": reason, "confirmed": False,
                           "detail": "cancel call failed"}
            try:
                observe({"termination": termination})
            except Exception:
                pass
            return
        ack_state = ack.get("state") if type(ack) is dict else None
        confirmed = ack.get("confirmed") if type(ack) is dict else None
        if ack_state not in ("completed", "failed", "cancelled",
                             "timed_out"):
            try:
                observe({"cancel_ack_invalid": sanitize(str(ack), 200)})
            except Exception:
                pass
            ack_state = "cancelled"
            confirmed = False
        if type(confirmed) is not bool:
            confirmed = False
        job_state = ack_state
        termination = {"reason": reason, "ack_state": ack_state,
                       "confirmed": confirmed}
        try:
            observe({"termination": termination})
            receipt_store.record_state(project_id, job_id, ack_state,
                                       observed=True,
                                       note=f"cancel confirmed: {reason}")
        except Exception:
            pass
        try:
            latest = handle.status()
            if type(latest) is dict \
                    and type(latest.get("workspace")) is str:
                receipt_store.record_workspace(
                    project_id, job_id, latest["workspace"])
        except Exception:
            pass

    def backend_close() -> None:
        nonlocal job_state
        try:
            result = handle.close()
        except Exception as error:
            try:
                observe({"close_failed": sanitize(str(error))})
            except Exception:
                pass
            if job_state not in ("completed", "failed", "cancelled",
                                 "timed_out"):
                try:
                    receipt_store.mark_unresolved(
                        project_id, job_id,
                        f"close failed while job was {job_state};"
                        " outcome unknown")
                except Exception:
                    pass
            return
        if type(result) is not dict:
            try:
                observe({"close_invalid": True})
            except Exception:
                pass
            return
        receipts = result.get("receipts")
        kinds = []
        saw_truncated = False
        if type(receipts) is list:
            for item in receipts:
                if type(item) is not dict:
                    kinds.append("<invalid>")
                    continue
                kinds.append(sanitize(str(item.get("kind", "?")), 40))
                if item.get("truncated") is True:
                    saw_truncated = True
        try:
            claim({"close": {
                "state": sanitize(str(result.get("state")), 40),
                "receipt_kinds": kinds[:50],
                "receipt_count": len(kinds),
                "truncated": saw_truncated}})
        except Exception:
            pass
        close_state = result.get("state")
        if job_state in ("completed", "failed", "cancelled", "timed_out"):
            if close_state != job_state:
                try:
                    observe({"close_state_mismatch":
                             {"observed": job_state,
                              "claimed": sanitize(str(close_state), 40)}})
                except Exception:
                    pass
            return
        if close_state in ("completed", "failed", "cancelled",
                           "timed_out"):
            job_state = close_state
            try:
                receipt_store.record_state(
                    project_id, job_id, close_state, observed=True,
                    note="close forced terminal state")
            except Exception:
                pass
        else:
            try:
                observe({"close_state_invalid":
                         sanitize(str(close_state), 40)})
                receipt_store.mark_unresolved(
                    project_id, job_id,
                    "close returned a non-terminal state;"
                    " outcome unknown")
            except Exception:
                pass

    try:
        send_command("hello", {})
        started = await_types({"started"}, what="hello")
        try:
            claim({"hello": _scrub_facts(started.get("payload", {}),
                                         sanitize)})
        except Exception:
            pass
        try:
            receipt_store.record_state(project_id, job_id, "running",
                                       observed=True,
                                       note="worker started")
        except Exception as error:
            raise _JobFailed("receipt store failure:"
                             f" {sanitize(str(error))}") from error
        job_state = "running"
        for fault in untriggered:
            send_and_ack("set_fault",
                         {"name": fault["hook"],
                          "params": dict(fault["params"])},
                         f"fault {fault['hook']}")
        send_and_ack("reset", {"fixture": dict(effective["fixture"])},
                     "reset")
        if cancelled():
            status = "cancelled"
        else:
            driver.drive(effective["agent"], invoke,
                         dict(effective["fixture"]))
    except EnvCancelled:
        status = "cancelled"
    except BudgetExceeded:
        status = "budget_exhausted"
    except _JobTruncated as signal:
        status = "budget_exhausted"
        truncated = True
        try:
            observe({"truncated": sanitize(str(signal), 300)})
        except Exception:
            pass
    except _JobFailed as signal:
        status = "error"
        error_text = sanitize(str(signal), 500)
        termination = {"reason": "backend_failed"
                       if signal.kind != "protocol_violation"
                       else "protocol_violation",
                       "detail": sanitize(str(signal), 300)}
    except PaymentTimeout as error:
        status = "error"
        error_text = f"{type(error).__name__}: {sanitize(str(error))}"
    except ToolError as error:
        status = "error"
        error_text = f"{type(error).__name__}: {sanitize(str(error))}"
    except Exception as error:
        status = "error"
        error_text = f"{type(error).__name__}: {sanitize(str(error))}"

    completed_at = utcnow()
    wall_ms = int((time.monotonic() - started_mono) * 1000)

    if status == "completed":
        try:
            evidence = collect_evidence()
        except _JobTruncated as signal:
            evidence = {"snapshot": None, "trace": None}
            missing_evidence = True
            truncated = True
            status = "budget_exhausted"
            try:
                observe({"truncated": sanitize(str(signal), 300)})
            except Exception:
                pass
        except EnvCancelled:
            evidence = {"snapshot": None, "trace": None}
            missing_evidence = True
            status = "cancelled"
        except BudgetExceeded:
            evidence = {"snapshot": None, "trace": None}
            missing_evidence = True
            status = "budget_exhausted"
        except Exception as signal:
            evidence = {"snapshot": None, "trace": None}
            missing_evidence = True
            status = "error"
            error_text = ("evidence unavailable:"
                          f" {sanitize(str(signal), 300)}")
        if status == "completed":
            try:
                latest = handle.status()
                if type(latest) is dict and latest.get("state"):
                    claim({"status": _scrub_facts(
                        {"state": latest.get("state"),
                         "output_bytes": latest.get("output_bytes"),
                         "elapsed_ms": latest.get("elapsed_ms")},
                        sanitize)})
                    if type(latest.get("workspace")) is str:
                        receipt_store.record_workspace(
                            project_id, job_id, latest["workspace"])
            except Exception:
                pass
            try:
                send_command("close", {})
            except _JobFailed as signal:
                try:
                    observe({"close_send_failed":
                             sanitize(str(signal), 300)})
                except Exception:
                    pass
            else:
                try:
                    await_types({"completed"}, what="close",
                                budget_ms=CLOSE_POLL_MS)
                    job_state = "completed"
                except (_JobFailed, _JobTruncated, EnvCancelled,
                        BudgetExceeded) as signal:
                    try:
                        observe({"close_event_missing":
                                 sanitize(str(signal), 300)})
                    except Exception:
                        pass
                else:
                    try:
                        receipt_store.record_state(
                            project_id, job_id, "completed",
                            observed=True, note="worker completed")
                    except Exception as error:
                        try:
                            observe({"close_state_note_failed":
                                     sanitize(str(error), 200)})
                        except Exception:
                            pass
        else:
            cancel_job("budget_exhausted" if truncated else status)
        backend_close()
    elif status == "cancelled" and not invoked_any:
        try:
            evidence = collect_evidence()
        except Exception:
            evidence = {"snapshot": None, "trace": None}
            missing_evidence = True
        cancel_job("cancelled")
        backend_close()
    else:
        if status == "cancelled":
            cancel_job("cancelled")
        elif truncated:
            cancel_job("output_truncated")
            try:
                observe({"truncated": True})
            except Exception:
                pass
        elif saw_backend_timeout:
            cancel_job("backend_timed_out")
        elif wall_exceeded():
            cancel_job("wall_exhausted")
        elif status == "error" and isinstance(termination, dict) \
                and termination.get("reason"):
            cancel_job(termination["reason"])
        else:
            cancel_job(status)
        try:
            evidence = collect_evidence()
        except _JobTruncated as signal:
            evidence = {"snapshot": None, "trace": None}
            missing_evidence = True
            truncated = True
            try:
                observe({"truncated": sanitize(str(signal), 300)})
            except Exception:
                pass
        except Exception:
            evidence = {"snapshot": None, "trace": None}
            missing_evidence = True
        backend_close()

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
            "completed_at": completed_at,
            "backend": {"name": backend_name, "job_id": job_id,
                        "backend_version":
                        backend_pin["backend_version"],
                        "state": job_state, "termination": termination,
                        "truncated": truncated,
                        "missing_evidence": missing_evidence,
                        "output_bytes": output_bytes,
                        "events": event_seq, "commands": cmd_seq}}
    stored = store.save_run(project_id=project_id, resolved_id=resolved_id,
                            repetition=repetition, attempt=attempt,
                            body=body, created_at=completed_at)
    store.add_ledger(project_id=project_id, resolved_id=resolved_id,
                     run_id=stored["run_id"], resource=LEDGER_RESOURCE,
                     amount=dispatches, created_at=completed_at)
    return stored
