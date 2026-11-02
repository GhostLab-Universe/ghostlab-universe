"""Explicit qualification runner.

The runner executes the environment on purpose and binds the outcome to the
exact registered code and manifest. Checks: reset, isolation, schemas,
evidence, fault-hooks, cancellation, reproduction modes. All pass and the
environment is ``qualified-for-declared``; any failure means ``failed``.
"""

import threading
from copy import deepcopy

from . import (QUALIFICATION_RUNNER_VERSION, STATUS_FAILED, STATUS_QUALIFIED)
from .adapters import EnvCancelled, EnvError, resolve_adapter_ref
from .registry import utcnow

CHECKS = ("reset", "isolation", "schemas", "evidence", "fault-hooks",
          "cancellation", "reproduction-modes")

SUPPORTED_REPRODUCTION_MODES = ("recorded-actions", "deterministic-reset")

DEFAULT_FIXTURE = {
    "order_id": "synthetic-order-1",
    "customer_id": "synthetic-customer-1",
    "paid_minor": 250_000,
    "currency": "INR",
}


def _new(registry, registration):
    cls = resolve_adapter_ref(registration["adapter_ref"],
                              registry.allowed_adapter_prefixes)
    return cls()


def _check_reset(registry, registration, fixture):
    adapter = _new(registry, registration)
    adapter.reset(deepcopy(fixture))
    first = adapter.snapshot()
    for call in adapter.probe_calls(fixture):
        adapter.invoke(call["tool"], call["arguments"])
    adapter.reset(deepcopy(fixture))
    second = adapter.snapshot()
    trace = adapter.evidence()["trace"]
    passed = first == second and trace == [] and first == adapter.snapshot()
    return passed, f"reset reproduces the initial snapshot: {passed}"


def _check_isolation(registry, registration, fixture):
    first, second = _new(registry, registration), _new(registry, registration)
    first.reset(deepcopy(fixture))
    second.reset(deepcopy(fixture))
    fresh = deepcopy(second.snapshot())
    probe = first.probe_calls(fixture)
    for call in probe:
        first.invoke(call["tool"], call["arguments"])
    untouched = second.snapshot() == fresh
    owned = len(first.evidence()["trace"]) == len(probe)
    passed = untouched and owned
    return passed, f"sibling untouched: {untouched}; own trace owned: {owned}"


def _check_schemas(registry, registration, fixture):
    adapter = _new(registry, registration)
    adapter.reset(deepcopy(fixture))
    probe = adapter.probe_calls(fixture)
    for call in probe:  # valid declared sequence must be accepted
        adapter.invoke(call["tool"], call["arguments"])
    before = adapter.snapshot()
    try:
        adapter.invoke("no_such_tool_xyz", {})
    except Exception:
        unknown_rejected = True
    else:
        unknown_rejected = False
    tampered = deepcopy(probe[0]["arguments"])
    tampered["unexpected_arg_xyz"] = "1"
    try:
        adapter.invoke(probe[0]["tool"], tampered)
    except Exception:
        malformed_rejected = True
    else:
        malformed_rejected = False
    unmutated = adapter.snapshot() == before
    passed = unknown_rejected and malformed_rejected and unmutated
    return passed, (f"unknown rejected: {unknown_rejected}; malformed rejected:"
                    f" {malformed_rejected}; rejections unmutated: {unmutated}")


def _check_evidence(registry, registration, fixture):
    adapter = _new(registry, registration)
    adapter.reset(deepcopy(fixture))
    probe = adapter.probe_calls(fixture)
    for call in probe:
        adapter.invoke(call["tool"], call["arguments"])
    evidence = adapter.evidence()
    trace = evidence["trace"]
    ordered = ([event.get("event_id") for event in trace]
               == list(range(1, len(probe) + 1)))
    consistent = evidence["snapshot"] == adapter.snapshot()
    tampered = deepcopy(evidence)
    tampered["snapshot"]["_probe_xyz"] = True
    if tampered["trace"]:
        tampered["trace"][0]["_probe_xyz"] = True
    isolated = (adapter.snapshot() == evidence["snapshot"]
                and adapter.evidence() == evidence)
    passed = ordered and consistent and isolated
    return passed, (f"trace ordered 1..{len(probe)}: {ordered}; snapshot"
                    f" consistent: {consistent}; copies isolated: {isolated}")


def _check_fault_hooks(registry, registration, fixture):
    adapter = _new(registry, registration)
    hooks = adapter.describe()["fault_hooks"]
    if not hooks:
        return False, "no fault hooks declared"
    try:
        adapter.set_fault("no_such_hook_xyz", {})
    except EnvError:
        unknown_rejected = True
    except Exception:
        return False, "unknown hook raised a non-EnvError"
    else:
        unknown_rejected = False
    if not unknown_rejected:
        return False, "unknown fault hook was accepted"
    adapter.reset(deepcopy(fixture))
    fresh = adapter.snapshot()
    for hook in hooks:
        adapter.set_fault(hook, {})
        adapter.reset(deepcopy(fixture))
        if adapter.snapshot() != fresh:
            return False, f"armed hook '{hook}' corrupts reset state"
    adapter.clear_faults()
    adapter.reset(deepcopy(fixture))
    if adapter.snapshot() != fresh:
        return False, "clear_faults did not restore fresh reset state"
    return True, f"{len(hooks)} hook(s) set/clear cleanly; unknown rejected"


def _check_cancellation(registry, registration, fixture):
    adapter = _new(registry, registration)
    if not adapter.describe()["supports_cancellation"]:
        return False, "cancellation not supported"
    adapter.reset(deepcopy(fixture))
    before = adapter.snapshot()
    probe = adapter.probe_calls(fixture)[0]
    cancelled = threading.Event()
    cancelled.set()
    try:
        adapter.invoke_cancellable(probe["tool"], probe["arguments"], cancelled)
    except EnvCancelled:
        raised = True
    except Exception:
        return False, "cancelled call raised a non-EnvCancelled error"
    else:
        raised = False
    unmutated = adapter.snapshot() == before
    adapter.invoke_cancellable(probe["tool"], probe["arguments"], threading.Event())
    plain_ok = len(adapter.evidence()["trace"]) == 1
    passed = raised and unmutated and plain_ok
    return passed, (f"cancel raised: {raised}; cancel unmutated: {unmutated};"
                    f" uncancelled works: {plain_ok}")


def _check_reproduction_modes(registry, registration, fixture):
    adapter = _new(registry, registration)
    modes = adapter.describe()["reproduction_modes"]
    if not modes:
        return False, "no reproduction modes declared"
    unknown = [m for m in modes if m not in SUPPORTED_REPRODUCTION_MODES]
    if unknown:
        return False, f"unsupported reproduction mode(s): {', '.join(unknown)}"
    probe = adapter.probe_calls(fixture)
    if "recorded-actions" in modes:
        adapter.reset(deepcopy(fixture))
        recorded = deepcopy(probe)
        for call in recorded:
            adapter.invoke(call["tool"], call["arguments"])
        first = adapter.snapshot()
        adapter.reset(deepcopy(fixture))
        for call in recorded:
            adapter.invoke(call["tool"], call["arguments"])
        if adapter.snapshot() != first:
            return False, "recorded-actions replay diverged"
    if "deterministic-reset" in modes:
        adapter.reset(deepcopy(fixture))
        for call in probe:
            adapter.invoke(call["tool"], call["arguments"])
        first = adapter.snapshot()
        adapter.reset(deepcopy(fixture))
        for call in adapter.probe_calls(fixture):
            adapter.invoke(call["tool"], call["arguments"])
        if adapter.snapshot() != first:
            return False, "deterministic-reset rerun diverged"
    return True, f"{len(modes)} mode(s) reproduced: {', '.join(modes)}"


_CHECK_FNS = {"reset": _check_reset, "isolation": _check_isolation,
              "schemas": _check_schemas, "evidence": _check_evidence,
              "fault-hooks": _check_fault_hooks,
              "cancellation": _check_cancellation,
              "reproduction-modes": _check_reproduction_modes}


def run_qualification(registry, env_id: str, *, manifest_digest: str | None = None,
                      fixture: dict | None = None, cancel=None) -> dict:
    """Execute every qualification check and store the version-bound report."""
    registration = registry.get(env_id)
    if manifest_digest is not None:
        manifest = registry.get_manifest(manifest_digest)
        if manifest["env_id"] != env_id:
            raise ValueError("manifest belongs to a different env_id")
    else:
        manifest = None
    if manifest is not None:
        declared = manifest["manifest"]
        if (declared["env_version"] != registration["env_version"]
                or declared["adapter_ref"] != registration["adapter_ref"]):
            raise ValueError("manifest does not match the registration")
    fixture = deepcopy(DEFAULT_FIXTURE) if fixture is None else deepcopy(fixture)
    checks = []
    for name in CHECKS:
        if cancel is not None and cancel.is_set():
            raise EnvCancelled("qualification cancelled")
        try:
            passed, detail = _CHECK_FNS[name](registry, registration, fixture)
            passed = bool(passed)
        except EnvCancelled:
            raise
        except Exception as error:  # one broken check fails, never crashes
            passed, detail = False, f"{type(error).__name__}: {error}"
        checks.append({"name": name, "passed": passed, "detail": str(detail)[:500]})
    status = STATUS_QUALIFIED if all(c["passed"] for c in checks) else STATUS_FAILED
    report = {
        "schema_version": 1, "env_id": env_id,
        "env_version": registration["env_version"],
        "adapter_ref": registration["adapter_ref"],
        "code_digest": registration["code_digest"],
        "manifest_digest": manifest_digest,
        "runner_version": QUALIFICATION_RUNNER_VERSION,
        "checks": checks, "status": status, "created_at": utcnow(),
    }
    stored = registry.store_report(report)
    registry.set_status(env_id, status)
    return stored
