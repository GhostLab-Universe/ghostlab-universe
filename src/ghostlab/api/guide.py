"""Map the guided UI's EffectiveConfig onto Track C requested configs.

Every ``EffectiveConfig`` leaf field is accounted for: directly mapped,
derived, defaulted with a note, recorded as metadata, or verified against
the registry. Nothing is silently dropped, clamped or raised. Unmappable
inputs raise :class:`ConfigError` (the service maps that to HTTP 400).

Preserved exactly: agent, fixture, faults, rules (invariants) and the
per-run / wall-clock limits. Defaulted with notes: ``repetitions.count``
(the UI carries no repetition count, so a single-run group is used),
``limits.max_total_tool_calls`` (derived as ``maxToolCalls`` x 1
repetition) and the requested ``name`` (derived from ``objective``).
Recorded as metadata (validated, echoed in exports, not executed by the
Track C controller): agent note, objective text, success criteria,
model/token/parallel/cost limits and the schema / env-version / adapter
pins. ``env.manifestDigest`` is pinned through ``resolve_config``'s
``manifest_digest`` argument, not stored in the requested body.

Pure except for the optional registry pin check: with ``registry=None``
the mapping never imports adapter code; with a registry it only reads the
registration record (no ``describe()``, no execution) to verify
``env.envVersion`` / ``env.adapterRef``.
"""

import re
from copy import deepcopy

from ..experiments.configs import (ConfigError, scan_for_secrets,
                                   validate_requested)

GUIDE_POLICY_VERSION = "guide-service/1"

# Maximal Track C ceilings so the guide policy never rejects beyond what
# Track C itself rejects. Requested limits above these bounds fail in
# validate_requested with an explicit error, never clamped.
DEFAULT_POLICY = {
    "policy_version": GUIDE_POLICY_VERSION,
    "ceiling_tool_calls": 1000,
    "ceiling_total_tool_calls": 25000,
    "ceiling_repetitions": 25,
    "ceiling_wall_clock_ms": 3600000,
    "allowed_agents": ["vulnerable", "repaired"],
    "allowed_hooks": ["timeout_after_commit"],
}

_ENV_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ENV_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+*-]{0,63}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
INVARIANTS = ("authorized_refunds", "total_refund_within_payment",
              "no_duplicate_full_refund", "unique_idempotency_keys")

_TOP_KEYS = ("schemaVersion", "env", "agent", "fixture", "faults", "checks",
             "limits", "objective", "successCriteria")
_ENV_KEYS = ("envId", "envVersion", "adapterRef", "manifestDigest")
_AGENT_KEYS = ("adapter", "note")
_FIXTURE_KEYS = ("orderId", "customerId", "paidMinor", "currency")
_FAULTS_KEYS = ("timeoutAfterCommit",)
_CHECKS_KEYS = ("invariants",)
_LIMITS_KEYS = ("maxToolCalls", "maxModelCalls", "maxInputTokens",
                "maxOutputTokens", "maxWallClockMs", "maxParallelRuns",
                "maxCostMinor", "costCurrency")


def _is_int(value) -> bool:
    return type(value) is int


def _check_keys(obj, keys, path, errors) -> None:
    extra = [k for k in obj if k not in keys]
    for key in extra:
        where = f"{path}.{key}" if path else str(key)
        errors.append(f"{where}: unknown field")
    for key in keys:
        if key not in obj:
            where = f"{path}.{key}" if path else str(key)
            errors.append(f"{where}: missing field")


def _check_id(value, limit) -> bool:
    return type(value) is str and 1 <= len(value) <= limit


def validate_effective_config(obj) -> dict:
    """Validate an EffectiveConfig dict, mirroring web/src/config.ts rules."""
    errors: list = []
    if type(obj) is not dict:
        raise ConfigError("effective_config must be an object")
    _check_keys(obj, _TOP_KEYS, "", errors)
    if obj.get("schemaVersion") is not True and obj.get("schemaVersion") != 1:
        errors.append("schemaVersion: must be the integer 1")
    elif not _is_int(obj.get("schemaVersion")):
        errors.append("schemaVersion: must be the integer 1")

    env = obj.get("env")
    if type(env) is not dict:
        errors.append("env: must be an object")
    else:
        _check_keys(env, _ENV_KEYS, "env", errors)
        if type(env.get("envId")) is not str \
                or not _ENV_ID_RE.match(env.get("envId") or ""):
            errors.append("env.envId: invalid env_id")
        if type(env.get("envVersion")) is not str \
                or not _ENV_VERSION_RE.match(env.get("envVersion") or ""):
            errors.append("env.envVersion: invalid env_version")
        ref = env.get("adapterRef")
        if not _check_id(ref, 200) or " " in (ref or "") \
                or (ref or "").count(":") != 1:
            errors.append("env.adapterRef: must look like 'module:Class'")
        digest = env.get("manifestDigest")
        if digest is not None and (
                type(digest) is not str or not _DIGEST_RE.match(digest)):
            errors.append("env.manifestDigest:"
                          " must be null or a sha256 hex digest")

    agent = obj.get("agent")
    if type(agent) is not dict:
        errors.append("agent: must be an object")
    else:
        _check_keys(agent, _AGENT_KEYS, "agent", errors)
        if agent.get("adapter") not in ("vulnerable", "repaired"):
            errors.append("agent.adapter: must be vulnerable or repaired")
        note = agent.get("note")
        if type(note) is not str or len(note) > 2000:
            errors.append("agent.note:"
                          " must be a string of at most 2000 chars")

    fixture = obj.get("fixture")
    if type(fixture) is not dict:
        errors.append("fixture: must be an object")
    else:
        _check_keys(fixture, _FIXTURE_KEYS, "fixture", errors)
        if not _check_id(fixture.get("orderId"), 100):
            errors.append("fixture.orderId: must be 1-100 chars")
        if not _check_id(fixture.get("customerId"), 100):
            errors.append("fixture.customerId: must be 1-100 chars")
        if not _check_id(fixture.get("currency"), 100):
            errors.append("fixture.currency: must be 1-100 chars")
        paid = fixture.get("paidMinor")
        if not _is_int(paid) or not 1 <= paid <= 10**12:
            errors.append("fixture.paidMinor:"
                          " must be an integer 1..1000000000000")

    faults = obj.get("faults")
    if type(faults) is not dict:
        errors.append("faults: must be an object")
    else:
        _check_keys(faults, _FAULTS_KEYS, "faults", errors)
        if type(faults.get("timeoutAfterCommit")) is not bool:
            errors.append("faults.timeoutAfterCommit: must be a boolean")

    checks = obj.get("checks")
    if type(checks) is not dict:
        errors.append("checks: must be an object")
    else:
        _check_keys(checks, _CHECKS_KEYS, "checks", errors)
        invariants = checks.get("invariants")
        if type(invariants) is not list or not 1 <= len(invariants) <= 4:
            errors.append("checks.invariants: must list 1-4 invariants")
        else:
            seen = set()
            for name in invariants:
                if name not in INVARIANTS or name in seen:
                    errors.append("checks.invariants:"
                                  f" invalid or duplicate invariant"
                                  f" {str(name)!r}")
                    break
                seen.add(name)

    limits = obj.get("limits")
    if type(limits) is not dict:
        errors.append("limits: must be an object")
    else:
        _check_keys(limits, _LIMITS_KEYS, "limits", errors)
        tool_calls = limits.get("maxToolCalls")
        if not _is_int(tool_calls) or not 1 <= tool_calls <= 1000:
            errors.append("limits.maxToolCalls:"
                          " must be an integer 1..1000")
        for key, low, high in (
                ("maxModelCalls", 0, 10**12),
                ("maxInputTokens", 0, 10**12),
                ("maxOutputTokens", 0, 10**12),
                ("maxWallClockMs", 1, 10**12),
                ("maxParallelRuns", 1, 64)):
            value = limits.get(key)
            if not _is_int(value) or not low <= value <= high:
                errors.append(f"limits.{key}:"
                              f" must be an integer {low}..{high}")
        cost = limits.get("maxCostMinor")
        currency = limits.get("costCurrency")
        if cost is not None and (
                not _is_int(cost) or not 0 <= cost <= 10**15):
            errors.append("limits.maxCostMinor: must be null or an integer"
                          " 0..1000000000000000")
        if currency is not None and (
                type(currency) is not str
                or not _CURRENCY_RE.match(currency)):
            errors.append("limits.costCurrency:"
                          " must be null or a 3-letter code")
        if (cost is None) != (currency is None):
            errors.append("limits.maxCostMinor: cost amount and currency are"
                          " set together or both null")

    objective = obj.get("objective")
    if type(objective) is not str or not 1 <= len(objective) <= 2000:
        errors.append("objective: must be 1-2000 chars")
    criteria = obj.get("successCriteria")
    if type(criteria) is not list or not 1 <= len(criteria) <= 16:
        errors.append("successCriteria: must list 1-16 criteria")
    elif any(type(item) is not str or not 1 <= len(item) <= 200
             for item in criteria):
        errors.append("successCriteria: each criterion must be 1-200 chars")

    if errors:
        raise ConfigError("invalid effective_config: "
                          + "; ".join(errors))
    body = deepcopy(obj)
    scan_for_secrets(body, "effective_config")
    return body


def _note(effective_path, requested_path, action, detail) -> dict:
    return {"effective_path": effective_path,
            "requested_path": requested_path, "action": action,
            "detail": detail}


def map_effective_to_requested(effective_obj, registry=None) -> dict:
    """Validate and map an EffectiveConfig to a Track C requested body.

    Returns ``{"effective", "requested", "policy", "manifest_digest",
    "mapping_notes"}``. With ``registry`` given, ``env.envVersion`` and
    ``env.adapterRef`` are verified against the stored registration and
    mismatches raise :class:`ConfigError`.
    """
    effective = validate_effective_config(effective_obj)
    env = effective["env"]
    agent = effective["agent"]
    fixture = effective["fixture"]
    faults = effective["faults"]
    checks = effective["checks"]
    limits = effective["limits"]
    objective = effective["objective"]

    if registry is not None:
        try:
            registration = registry.get(env["envId"])
        except ValueError as error:
            raise ConfigError(f"unknown env_id {env['envId']!r}") from error
        if env["envVersion"] != registration["env_version"]:
            raise ConfigError(
                "env.envVersion does not match the registration"
                f" ({registration['env_version']!r}); re-select the"
                " environment instead of editing the version")
        if env["adapterRef"] != registration["adapter_ref"]:
            raise ConfigError(
                "env.adapterRef does not match the registration;"
                " re-select the environment instead of editing the adapter")

    if len(objective) <= 100:
        name = objective
        name_detail = ("requested.name mapped from the full objective"
                       " (1-100 chars)")
    else:
        name = objective[:100]
        name_detail = ("requested.name derived from the first 100 chars of"
                       " the objective; the full text stays in"
                       " effective_config.objective")

    if faults["timeoutAfterCommit"]:
        requested_faults = [{"hook": "timeout_after_commit", "params": {},
                             "required": True}]
        fault_detail = ("faults.timeoutAfterCommit=true maps to one required"
                        " 'timeout_after_commit' fault; a run whose required"
                        " fault never fires is a fail, never a pass")
    else:
        requested_faults = []
        fault_detail = ("faults.timeoutAfterCommit=false maps to an empty"
                        " fault list")

    requested = {
        "config_version": 1,
        "name": name,
        "env_id": env["envId"],
        "agent": agent["adapter"],
        "fixture": {"order_id": fixture["orderId"],
                    "customer_id": fixture["customerId"],
                    "paid_minor": fixture["paidMinor"],
                    "currency": fixture["currency"]},
        "faults": requested_faults,
        "rules": list(checks["invariants"]),
        "limits": {"max_tool_calls": limits["maxToolCalls"],
                   "max_total_tool_calls": limits["maxToolCalls"],
                   "max_wall_clock_ms": limits["maxWallClockMs"]},
        "repetitions": {"count": 1},
    }
    policy = deepcopy(DEFAULT_POLICY)
    # Unmappable combinations (per-run floor, Track C wall-clock ceiling)
    # raise here with an explicit error, never adjusted silently.
    validate_requested(requested, policy)

    if registry is not None:
        pin_detail_version = ("env.envVersion verified against the stored"
                              " registration")
        pin_detail_ref = ("env.adapterRef verified against the stored"
                          " registration")
    else:
        pin_detail_version = ("env.envVersion recorded; verified against the"
                              " registration at submit time")
        pin_detail_ref = ("env.adapterRef recorded; verified against the"
                          " registration at submit time")
    notes = [
        _note("schemaVersion", "config_version", "verified",
              "schemaVersion 1 maps to requested config_version 1"),
        _note("env.envId", "env_id", "mapped",
              "env.envId maps exactly to requested env_id"),
        _note("env.envVersion", None, "verified", pin_detail_version),
        _note("env.adapterRef", None, "verified", pin_detail_ref),
        _note("env.manifestDigest", None, "mapped",
              "env.manifestDigest is pinned via resolve manifest_digest,"
              " not stored in the requested body"),
        _note("agent.adapter", "agent", "mapped",
              "agent.adapter maps exactly to requested agent"),
        _note("agent.note", None, "metadata",
              "agent.note is validated and recorded with the submission;"
              " Track C executes no free-form note"),
        _note("fixture.orderId", "fixture.order_id", "mapped",
              "fixture.orderId maps exactly to fixture.order_id"),
        _note("fixture.customerId", "fixture.customer_id", "mapped",
              "fixture.customerId maps exactly to fixture.customer_id"),
        _note("fixture.paidMinor", "fixture.paid_minor", "mapped",
              "fixture.paidMinor maps exactly to fixture.paid_minor"),
        _note("fixture.currency", "fixture.currency", "mapped",
              "fixture.currency maps exactly to fixture.currency"),
        _note("faults.timeoutAfterCommit", "faults", "mapped", fault_detail),
        _note("checks.invariants", "rules", "mapped",
              "checks.invariants map exactly to requested rules, order"
              " preserved"),
        _note("limits.maxToolCalls", "limits.max_tool_calls", "mapped",
              "limits.maxToolCalls maps exactly to max_tool_calls"),
        _note("limits.maxToolCalls", "limits.max_total_tool_calls",
              "derived",
              "max_total_tool_calls derived as maxToolCalls x 1 repetition"
              " (single-run group); a maxToolCalls of 1 is unmappable"
              " because a refund attempt needs at least get_order +"
              " issue_refund"),
        _note("limits.maxWallClockMs", "limits.max_wall_clock_ms", "mapped",
              "limits.maxWallClockMs maps exactly to max_wall_clock_ms;"
              " values above the Track C 3,600,000 ms ceiling are"
              " unmappable, never clamped"),
        _note("limits.maxModelCalls", None, "metadata",
              "maxModelCalls is validated and recorded; local execution"
              " records model_calls 0"),
        _note("limits.maxInputTokens", None, "metadata",
              "maxInputTokens is validated and recorded; local execution"
              " tracks no token usage"),
        _note("limits.maxOutputTokens", None, "metadata",
              "maxOutputTokens is validated and recorded; local execution"
              " tracks no token usage"),
        _note("limits.maxParallelRuns", None, "metadata",
              "maxParallelRuns is validated and recorded; the service"
              " executes each run-group serially"),
        _note("limits.maxCostMinor", None, "metadata",
              "maxCostMinor is validated and recorded; local execution"
              " tracks no cost"),
        _note("limits.costCurrency", None, "metadata",
              "costCurrency is validated and recorded; local execution"
              " tracks no cost"),
        _note("objective", "name", "derived", name_detail),
        _note("objective", None, "metadata",
              "the full objective text is recorded in"
              " effective_config.objective"),
        _note("successCriteria", None, "metadata",
              "successCriteria are validated and recorded; verdicts come"
              " from invariant checks over executed evidence, never from"
              " matching criteria text"),
        _note("(repetitions)", "repetitions.count", "defaulted",
              "EffectiveConfig carries no repetition count; defaulted to a"
              " single-run group (count 1)"),
        _note("(policy)", None, "defaulted",
              f"policy {GUIDE_POLICY_VERSION} applied with maximal Track C"
              " ceilings; requested limits above a ceiling are rejected,"
              " never clamped or raised"),
    ]
    return {"effective": effective, "requested": requested, "policy": policy,
            "manifest_digest": env["manifestDigest"],
            "mapping_notes": notes}
