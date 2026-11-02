"""Requested and resolved experiment configs, generic over P1 worlds.

A *requested* config is client-supplied reusable settings: world, target,
fixture, faults, rules, limits and repetitions. A *resolved* config is the
immutable binding of one request to pinned versions, effective values and
provenance. Resolution never silently drops, clamps or raises anything:
unsupported settings and impossible combinations are rejected explicitly.

Purity:

- :func:`validate_policy` and :func:`validate_requested` are pure
  functions of their inputs. They take no registry, import no adapter
  code, make no model calls and mutate nothing.
- :func:`resolve_requested` reads the operator registry and runs
  describe-only capability discovery. It never resets, invokes, faults
  or otherwise mutates the target environment, and never calls a model.

Policy ceilings come from the caller (the deployment policy owner). A
requested limit above its ceiling is rejected, never clamped.
"""

import hashlib
import json
import re
from copy import deepcopy

from ..extensions.manifest import NAME_PATTERN, WORLD_PATTERN

CONFIG_VERSION = 1
EXPERIMENTS_SCHEMA_VERSION = 1
CONTROLLER_VERSION = "experiment-controller/1"
MAX_CONFIG_BYTES = 262_144

MIN_TOOL_CALLS_PER_RUN = 1
MAX_TOOL_CALLS_PER_RUN = 1000
MIN_TOTAL_TOOL_CALLS = 1
MAX_TOTAL_TOOL_CALLS = 25000
MIN_REPETITIONS = 1
MAX_REPETITIONS = 25
MIN_WALL_CLOCK_MS = 1
MAX_WALL_CLOCK_MS = 3_600_000
MAX_FAULTS = 8
MAX_RULES = 32
MAX_ACTIONS = 100

HOOK_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TOOL_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
POLICY_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
PROJECT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_REQUESTED_KEYS = {"config_version", "name", "world_version", "target", "fixture",
                   "faults", "rules", "limits", "repetitions"}
_TARGET_KEYS = {"kind", "id", "actions"}
_FIXTURE_KEYS = {"name"}
_FAULT_KEYS = {"hook", "params", "required"}
_LIMITS_KEYS = {"max_tool_calls", "max_total_tool_calls", "max_wall_clock_ms"}
_REPETITIONS_KEYS = {"count"}
_POLICY_KEYS = {"policy_version", "ceiling_tool_calls", "ceiling_total_tool_calls",
                "ceiling_repetitions", "ceiling_wall_clock_ms", "allowed_targets",
                "allowed_hooks"}
_RESOLVED_KEYS = {"schema_version", "resolved_config_id", "project_id",
                  "requested_config_id", "requested_digest", "resolved_at",
                  "resolved_by", "pins", "effective", "provenance", "digest"}
_PINS_KEYS = {"world_version", "environment_id", "environment_version",
              "adapter_ref", "code_digest", "manifest_digest", "target_id",
              "target_version", "script_digest", "controller_version",
              "discovered_tools", "discovered_modes", "discovered_fixtures"}
_EFFECTIVE_KEYS = {"target", "fixture", "scenario", "faults", "rules", "limits",
                   "repetitions"}
_PROVENANCE_KEYS = {"policy_version", "requested_by", "resolved_by",
                    "capability_source", "adapter_origin", "qualification_status"}


class ConfigError(ValueError):
    """A requested, policy or resolved config is invalid or unsupported."""


def canonical_digest(body: dict) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


_SECRET_MARKERS = ("API_KEY", "APIKEY", "PRIVATE KEY", "client_secret",
                   "BEARER ", "xox-")


def scan_for_secrets(node, path: str = "config") -> None:
    """Reject secret-like text anywhere in a config object (pure)."""
    if isinstance(node, str):
        for marker in _SECRET_MARKERS:
            if marker in node:
                raise ConfigError(
                    f"{path} must not carry secret-like "
                    f"text (marker {marker.strip()!r}); configs hold no secrets")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            scan_for_secrets(item, f"{path}[{index}]")
    elif isinstance(node, dict):
        for key, item in node.items():
            scan_for_secrets(item, f"{path}.{key}")


def check_project_id(project_id: str) -> str:
    if type(project_id) is not str or not PROJECT_PATTERN.match(project_id):
        raise ConfigError("invalid project_id")
    return project_id


def requested_config_id(project_id: str, body: dict) -> str:
    """Content-addressed id, so identical creates are idempotent per project."""
    return "req-" + canonical_digest(
        {"project_id": project_id, "config": body})[:16]


def resolved_config_id(project_id: str, requested_id: str, stable: dict) -> str:
    """Content-addressed id over the stable resolution surface.

    Identity, timestamps, digests and the resolving actor are excluded,
    so re-resolving an unchanged request returns the same id. The pinned
    qualification status IS included: resolving after qualification is a
    different effective binding and gets a different id.
    """
    return "res-" + canonical_digest(
        {"project_id": project_id, "requested_config_id": requested_id,
         "resolved": stable})[:16]

def _short(value, label: str, maximum=200) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ConfigError(f"invalid {label}")
    return value


def _bounded_int(value, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"invalid {label}")
    return value


def validate_policy(obj) -> dict:
    """Validate a deployment-owned policy ceiling object (pure)."""
    if type(obj) is not dict or set(obj) != _POLICY_KEYS:
        raise ConfigError("policy must carry exactly the v1 policy fields")
    version = obj["policy_version"]
    if type(version) is not str or not POLICY_VERSION_PATTERN.match(version):
        raise ConfigError("invalid policy_version")
    ceilings = {
        "ceiling_tool_calls": _bounded_int(obj["ceiling_tool_calls"], "ceiling",
                                           MIN_TOOL_CALLS_PER_RUN,
                                           MAX_TOOL_CALLS_PER_RUN),
        "ceiling_total_tool_calls": _bounded_int(obj["ceiling_total_tool_calls"],
                                                 "ceiling", MIN_TOTAL_TOOL_CALLS,
                                                 MAX_TOTAL_TOOL_CALLS),
        "ceiling_repetitions": _bounded_int(obj["ceiling_repetitions"], "ceiling",
                                            MIN_REPETITIONS, MAX_REPETITIONS),
        "ceiling_wall_clock_ms": _bounded_int(obj["ceiling_wall_clock_ms"],
                                              "ceiling", MIN_WALL_CLOCK_MS,
                                              MAX_WALL_CLOCK_MS),
    }
    targets = obj["allowed_targets"]
    if (type(targets) is not list or len(targets) > 64
            or any(type(entry) is not str or not entry or len(entry) > 200
                   for entry in targets)
            or len(set(targets)) != len(targets)):
        raise ConfigError("invalid allowed_targets")
    hooks = obj["allowed_hooks"]
    if (type(hooks) is not list or len(hooks) > 64
            or any(type(entry) is not str or not HOOK_PATTERN.match(entry)
                   for entry in hooks)
            or len(set(hooks)) != len(hooks)):
        raise ConfigError("invalid allowed_hooks")
    return {"policy_version": version, **ceilings,
            "allowed_targets": list(targets), "allowed_hooks": list(hooks)}


def _check_target(target, path: str) -> dict:
    if type(target) is not dict or "kind" not in target:
        raise ConfigError(f"{path} needs a target kind")
    kind = target["kind"]
    if kind == "registered":
        if set(target) != {"kind", "id"}:
            raise ConfigError(f"{path} registered targets carry kind and id")
        return {"kind": kind, "id": _short(target["id"], f"{path}.id")}
    if kind == "script":
        if set(target) != {"kind", "actions"}:
            raise ConfigError(f"{path} script targets carry kind and actions")
        actions = target["actions"]
        if type(actions) is not list or not 1 <= len(actions) <= MAX_ACTIONS:
            raise ConfigError(f"{path} script holds 1 to 100 actions")
        clean = []
        for index, item in enumerate(actions):
            where = f"{path}.actions[{index}]"
            if type(item) is not dict or set(item) != {"tool", "arguments"}:
                raise ConfigError(f"{where} needs exactly tool and arguments")
            if type(item["tool"]) is not str or not TOOL_PATTERN.match(item["tool"]):
                raise ConfigError(f"invalid {where}.tool")
            if type(item["arguments"]) is not dict:
                raise ConfigError(f"invalid {where}.arguments")
            try:
                arguments = json.loads(json.dumps(item["arguments"]))
            except (TypeError, ValueError):
                raise ConfigError(f"{where}.arguments must be JSON") from None
            clean.append({"tool": item["tool"], "arguments": arguments})
        try:
            script_text = json.dumps({"actions": clean}, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False)
        except ValueError:
            raise ConfigError(f"{path} script must be canonical JSON") from None
        if len(script_text.encode()) > 32768:
            raise ConfigError(f"{path} script exceeds the size bound")
        return {"kind": kind, "actions": clean}
    raise ConfigError(f"{path}.kind must be registered or script")


def _check_faults(faults, path: str) -> list:
    if type(faults) is not list or len(faults) > MAX_FAULTS:
        raise ConfigError(f"{path} holds at most {MAX_FAULTS} faults")
    seen, clean = set(), []
    for index, entry in enumerate(faults):
        where = f"{path}[{index}]"
        if type(entry) is not dict or set(entry) != _FAULT_KEYS:
            raise ConfigError(f"{where} must carry exactly hook, params and required")
        hook = entry["hook"]
        if type(hook) is not str or not HOOK_PATTERN.match(hook):
            raise ConfigError(f"invalid {where}.hook")
        if hook in seen:
            raise ConfigError(f"duplicate fault hook '{hook}'")
        seen.add(hook)
        if type(entry["params"]) is not dict:
            raise ConfigError(f"invalid {where}.params")
        try:
            params = json.loads(json.dumps(entry["params"]))
        except (TypeError, ValueError):
            raise ConfigError(f"{where}.params must be JSON") from None
        if type(entry["required"]) is not bool:
            raise ConfigError(f"invalid {where}.required")
        clean.append({"hook": hook, "params": params, "required": entry["required"]})
    return clean


def validate_requested(obj, policy_obj) -> dict:
    """Validate reusable client-supplied experiment settings (pure).

    Unknown fields are rejected at every level; nothing is silently
    ignored. Impossible combinations and anything above policy ceilings
    are rejected, never adjusted.
    """
    if type(obj) is not dict or set(obj) != _REQUESTED_KEYS:
        raise ConfigError("requested config must carry exactly the v1 fields: "
                          f"{', '.join(sorted(_REQUESTED_KEYS))}")
    if type(obj["config_version"]) is not int or obj["config_version"] != CONFIG_VERSION:
        raise ConfigError("unsupported config_version")
    policy = validate_policy(policy_obj)
    name = _short(obj["name"], "name")
    world = obj["world_version"]
    if type(world) is not str or not WORLD_PATTERN.match(world):
        raise ConfigError("invalid world_version")
    target = _check_target(obj["target"], "target")
    fixture = obj["fixture"]
    if type(fixture) is not dict or set(fixture) != _FIXTURE_KEYS:
        raise ConfigError("fixture must carry exactly a name")
    fixture_name = _short(fixture["name"], "fixture.name")
    faults = _check_faults(obj["faults"], "faults")
    rules = obj["rules"]
    if (type(rules) is not list or len(rules) > MAX_RULES
            or any(type(name) is not str or not NAME_PATTERN.match(name)
                   for name in rules)
            or len(set(rules)) != len(rules)):
        raise ConfigError("invalid or duplicate rules")
    limits_obj = obj["limits"]
    if type(limits_obj) is not dict or set(limits_obj) != _LIMITS_KEYS:
        raise ConfigError("limits must carry exactly the v1 limit fields")
    limits = {
        "max_tool_calls": _bounded_int(limits_obj["max_tool_calls"], "limit",
                                       MIN_TOOL_CALLS_PER_RUN, MAX_TOOL_CALLS_PER_RUN),
        "max_total_tool_calls": _bounded_int(limits_obj["max_total_tool_calls"],
                                             "limit", MIN_TOTAL_TOOL_CALLS,
                                             MAX_TOTAL_TOOL_CALLS),
        "max_wall_clock_ms": _bounded_int(limits_obj["max_wall_clock_ms"], "limit",
                                          MIN_WALL_CLOCK_MS, MAX_WALL_CLOCK_MS),
    }
    repetitions_obj = obj["repetitions"]
    if type(repetitions_obj) is not dict or set(repetitions_obj) != _REPETITIONS_KEYS:
        raise ConfigError("repetitions must carry exactly a count")
    count = _bounded_int(repetitions_obj["count"], "count", MIN_REPETITIONS,
                         MAX_REPETITIONS)
    if limits["max_total_tool_calls"] < limits["max_tool_calls"] * count:
        raise ConfigError("aggregate tool budget cannot cover the repetitions")
    for key, ceiling in (("max_tool_calls", "ceiling_tool_calls"),
                         ("max_total_tool_calls", "ceiling_total_tool_calls"),
                         ("max_wall_clock_ms", "ceiling_wall_clock_ms")):
        if limits[key] > policy[ceiling]:
            raise ConfigError(f"{key} exceeds the policy ceiling")
    if count > policy["ceiling_repetitions"]:
        raise ConfigError("repetitions exceed the policy ceiling")
    if target["kind"] == "registered" and target["id"] not in policy["allowed_targets"]:
        raise ConfigError(f"target '{target['id']}' is not policy-allowed")
    if target["kind"] == "script" and "script" not in policy["allowed_targets"]:
        raise ConfigError("inline scripts are not policy-allowed")
    for fault in faults:
        if fault["hook"] not in policy["allowed_hooks"]:
            raise ConfigError(f"fault hook '{fault['hook']}' is not policy-allowed")
    return {"config_version": CONFIG_VERSION, "name": name, "world_version": world,
            "target": target, "fixture": {"name": fixture_name}, "faults": faults,
            "rules": list(rules), "limits": limits,
            "repetitions": {"count": count}}



def resolve_requested(requested, *, project_id: str, requested_id: str,
                      requested_digest: str, requested_by: str,
                      policy, operators, targets, manifest_digest=None,
                      resolved_by="controller", now_iso=None):
    """Bind a requested config to pinned versions (describe-only env access).

    Reads the operator registry and runs describe-only capability
    discovery on the registered environment. Instantiating the adapter
    for ``describe`` is capability discovery, not execution: resolution
    never resets, invokes, faults or closes a run, never starts the
    target agent, and never calls a model. Adapters whose code changed
    since review are refused, exactly like the execution gate.

    Qualification is RECORDED, not enforced, here: ``provenance``
    carries the qualification status of the pinned code digest, but
    resolving an unqualified environment is allowed (preview use-case).
    Execution rechecks qualification at start and refuses unqualified
    or failed environments.
    """
    check_project_id(project_id)
    if type(requested_id) is not str or not requested_id:
        raise ConfigError("invalid requested_id")
    if type(requested_by) is not str or not requested_by:
        raise ConfigError("invalid requested_by")
    if type(resolved_by) is not str or not resolved_by:
        raise ConfigError("invalid resolved_by")
    if manifest_digest is not None and (
            type(manifest_digest) is not str or not manifest_digest):
        raise ConfigError("invalid manifest_digest")
    clean_policy = validate_policy(policy)
    clean = validate_requested(requested, clean_policy)
    stamp = now_iso
    if stamp is not None and (type(stamp) is not str or not stamp):
        raise ConfigError("invalid resolved_at")
    from datetime import datetime, timezone
    stamp = stamp or datetime.now(timezone.utc).isoformat()

    try:
        record = operators.get(clean["world_version"])
    except LookupError:
        raise ConfigError(f"world '{clean['world_version']}' is unregistered"
                          ) from None
    try:
        describe = operators.describe_only(clean["world_version"])
    except ValueError as error:
        raise ConfigError(str(error)) from None

    if clean["fixture"]["name"] not in describe["fixtures"]:
        raise ConfigError(f"fixture '{clean['fixture']['name']}' is unsupported")
    for rule in clean["rules"]:
        if rule not in describe["invariants"]:
            raise ConfigError(f"rule '{rule}' is unsupported")
    fault_specs = {entry["id"]: entry for entry in describe["faults"]}
    for fault in clean["faults"]:
        spec = fault_specs.get(fault["hook"])
        if spec is None:
            raise ConfigError(f"fault hook '{fault['hook']}' is unsupported")
        declared = {item["name"] for item in spec.get("parameters", [])}
        for name in fault["params"]:
            if name not in declared:
                raise ConfigError(f"fault param '{name}' is undeclared")

    target = clean["target"]
    if target["kind"] == "registered":
        try:
            manifest = targets.target_manifest(clean["world_version"],
                                               target["id"])
        except (KeyError, LookupError):
            raise ConfigError(f"target '{target['id']}' is unregistered"
                              ) from None
        target_version = manifest.version
    else:
        declared_tools = {entry["name"] for entry in describe["tools"]}
        for index, action in enumerate(target["actions"]):
            if action["tool"] not in declared_tools:
                raise ConfigError(f"script action {index} targets "
                                  f"undeclared tool '{action['tool']}'")
        target_version = "inline-script/1"

    if manifest_digest is not None:
        try:
            manifest_body = operators.get_manifest(manifest_digest)
        except LookupError:
            raise ConfigError("unknown manifest digest") from None
        if manifest_body["world_version"] != clean["world_version"]:
            raise ConfigError("manifest names a different world")
        if manifest_body["adapter_ref"] != record["adapter_ref"]:
            raise ConfigError("manifest names a different adapter")

    scenario = {fault["hook"]: True for fault in clean["faults"]}
    for fault in clean["faults"]:
        for name, value in fault["params"].items():
            scenario[f"{fault['hook']}.{name}"] = value
    scenario["max_tool_calls"] = clean["limits"]["max_tool_calls"]

    resolved = {
        "schema_version": EXPERIMENTS_SCHEMA_VERSION,
        "resolved_config_id": "pending",
        "project_id": project_id,
        "requested_config_id": requested_id,
        "requested_digest": requested_digest,
        "resolved_at": stamp,
        "resolved_by": resolved_by,
        "pins": {
            "world_version": clean["world_version"],
            "environment_id": record["environment_id"],
            "environment_version": record["environment_version"],
            "adapter_ref": record["adapter_ref"],
            "code_digest": record["code_digest"],
            "manifest_digest": manifest_digest,
            "target_id": target.get("id", "inline-script"),
            "target_version": target_version,
            "script_digest": (canonical_digest({"actions": target["actions"]})
                              if target["kind"] == "script" else None),
            "controller_version": CONTROLLER_VERSION,
            "discovered_tools": sorted(entry["name"] for entry in describe["tools"]),
            "discovered_modes": list(describe["modes"]),
            "discovered_fixtures": sorted(describe["fixtures"]),
        },
        "effective": {
            "target": deepcopy(target),
            "fixture": dict(clean["fixture"]),
            "scenario": scenario,
            "faults": deepcopy(clean["faults"]),
            "rules": list(clean["rules"]),
            "limits": dict(clean["limits"]),
            "repetitions": dict(clean["repetitions"]),
        },
        "provenance": {
            "policy_version": clean_policy["policy_version"],
            "requested_by": requested_by,
            "resolved_by": resolved_by,
            "capability_source": "describe-only",
            "adapter_origin": "registered",
            "qualification_status": operators.qualification_status(
                clean["world_version"])["status"],
        },
    }
    stable_provenance = {key: value for key, value in resolved["provenance"].items()
                         if key != "resolved_by"}
    stable = {"requested_digest": resolved["requested_digest"],
              "pins": resolved["pins"], "effective": resolved["effective"],
              "provenance": stable_provenance}
    resolved["resolved_config_id"] = resolved_config_id(
        project_id, requested_id, stable)
    resolved["digest"] = canonical_digest(
        {key: value for key, value in resolved.items() if key != "digest"})
    return resolved

def validate_resolved(obj) -> dict:
    """Validate a stored or API-submitted resolved config (pure)."""
    if type(obj) is not dict or set(obj) != _RESOLVED_KEYS:
        raise ConfigError("resolved config must carry exactly the v1 fields")
    if obj["schema_version"] != EXPERIMENTS_SCHEMA_VERSION:
        raise ConfigError("unsupported resolved schema_version")
    for key in ("resolved_config_id", "project_id", "requested_config_id",
                "requested_digest", "resolved_at", "resolved_by"):
        if type(obj[key]) is not str or not obj[key]:
            raise ConfigError(f"invalid {key}")
    pins = obj["pins"]
    if type(pins) is not dict or set(pins) != _PINS_KEYS:
        raise ConfigError("resolved pins must carry exactly the v1 pin fields")
    for key in ("world_version", "environment_id", "environment_version",
                "adapter_ref", "code_digest", "target_id", "target_version",
                "controller_version"):
        if type(pins[key]) is not str or not pins[key]:
            raise ConfigError(f"invalid pins.{key}")
    if pins["manifest_digest"] is not None and (
            type(pins["manifest_digest"]) is not str
            or not pins["manifest_digest"]):
        raise ConfigError("invalid pins.manifest_digest")
    if pins["script_digest"] is not None and (
            type(pins["script_digest"]) is not str or not pins["script_digest"]):
        raise ConfigError("invalid pins.script_digest")
    if (type(pins["discovered_tools"]) is not list
            or type(pins["discovered_modes"]) is not list
            or type(pins["discovered_fixtures"]) is not list):
        raise ConfigError("invalid discovered capability lists")
    effective = obj["effective"]
    if type(effective) is not dict or set(effective) != _EFFECTIVE_KEYS:
        raise ConfigError("resolved effective must carry exactly the v1 fields")
    if type(effective["scenario"]) is not dict:
        raise ConfigError("invalid effective.scenario")
    provenance = obj["provenance"]
    if type(provenance) is not dict or set(provenance) != _PROVENANCE_KEYS:
        raise ConfigError("resolved provenance must carry exactly the v1 fields")
    if (type(obj["digest"]) is not str or obj["digest"] != canonical_digest(
            {key: value for key, value in obj.items() if key != "digest"})):
        raise ConfigError("resolved digest mismatch")
    return deepcopy(obj)


__all__ = ["ConfigError", "CONFIG_VERSION", "EXPERIMENTS_SCHEMA_VERSION",
           "CONTROLLER_VERSION", "MAX_CONFIG_BYTES", "canonical_digest",
           "check_project_id", "scan_for_secrets", "requested_config_id",
           "resolved_config_id", "validate_policy", "validate_requested",
           "resolve_requested", "validate_resolved"]