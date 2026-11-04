"""Requested and resolved experiment configs.

A *requested* config is client-supplied, reusable experiment settings: fixture,
faults, rules, limits and repetitions, plus the environment and agent to run.
A *resolved* config is the controller's immutable binding of one requested
config to pinned versions (environment, adapter code, world, adapter,
controller), effective values and provenance. Resolution never silently drops,
clamps or raises anything: unsupported settings and impossible combinations
are rejected with an explicit error.

Purity:

- :func:`validate_policy` and :func:`validate_requested` are pure functions of
  their inputs. They take no registry, import no adapter code, make no model
  calls and mutate nothing.
- :func:`resolve_requested` reads the environment registry and runs
  describe-only capability discovery. It never resets, invokes, faults or
  otherwise mutates the target environment, and never calls a model.

Policy ceilings come from the caller (the deployment's policy owner). A
requested limit above its ceiling is rejected, never clamped, and resolution
records the policy version without raising any ceiling.

Fault triggers (optional, backward compatible):

A fault may carry ``trigger`` = ``{"type", "tool", "occurrence"}``. ``type``
must be ``"occurrence"`` (anything else is rejected as unsupported, never
silently reinterpreted). ``tool`` is ``"*"`` (any tool: the occurrence counts
every dispatch) or one discovered tool name (the occurrence counts dispatches
of that tool only); resolution rejects names the environment does not
implement. ``occurrence`` is an int in 1..64: the fault arms immediately
before that counted dispatch and disarms right after, so it fires at most
once. Faults without ``trigger`` behave exactly as before (armed at reset,
firing on the first opportunity the hook allows).

Backend selection (optional, backward compatible):

A requested config may carry ``backend`` = ``{"name", "package"?}`` where
``package`` (when present) is exactly ``{"kind", "identity"}``. Omitted
backends resolve to ``trusted-local`` (the current in-process path, with
byte-identical behavior). Resolution pins ``backend`` =
``{"name", "backend_version", "package_kind"?, "package_identity"?}`` on
the resolved pins; unknown backend names are rejected at resolve time,
never silently remapped. Requested validation only checks the shape;
name validity is decided at resolve against the controller's backend
registry, without probing any backend (availability is an execution-time
refusal, never a resolve-time fallback).
"""

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass

from ..core import ADAPTER_VERSION, WORLD_VERSION
from ..envs.capabilities import discover
from ..envs.registry import utcnow

EXPERIMENTS_SCHEMA_VERSION = 1
CONTROLLER_VERSION = "experiment-controller/1"
CONFIG_VERSION = 1
MAX_CONFIG_BYTES = 262_144

# llm-* agents are model-driven (ghostlab.llm.agent); they need a bound client to run.
LLM_AGENTS = ("llm-naive", "llm-hardened", "llm-naive-noread", "llm-hardened-noread")
# Seeded-bug deterministic variants (benchmark targets; each driver lists its own).
SEEDED_AGENTS = ("status-ignored", "retry-nokey", "split-key-per-attempt", "double-submit")
AGENTS = ("vulnerable", "repaired", *LLM_AGENTS, *SEEDED_AGENTS)
INVARIANTS = ("authorized_refunds", "total_refund_within_payment",
              "no_duplicate_full_refund", "unique_idempotency_keys")

# A refund attempt needs at least get_order + issue_refund; anything lower can
# never execute, so it is an impossible combination, not a small budget.
MIN_TOOL_CALLS_PER_RUN = 2
MAX_TOOL_CALLS_PER_RUN = 1000
MIN_TOTAL_TOOL_CALLS = 2
MAX_TOTAL_TOOL_CALLS = 25000
MIN_REPETITIONS = 1
MAX_REPETITIONS = 25
MIN_WALL_CLOCK_MS = 1
MAX_WALL_CLOCK_MS = 3_600_000
MAX_FAULTS = 8
MAX_HOOKS = 16
MIN_TRIGGER_OCCURRENCE = 1
MAX_TRIGGER_OCCURRENCE = 64

PROJECT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ENV_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
HOOK_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
POLICY_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
BACKEND_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
BACKEND_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")

TRUSTED_LOCAL_BACKEND = "trusted-local"
TRUSTED_LOCAL_VERSION = "trusted-local/1"
EXECUTION_BACKEND_VERSION = "execution-backend/1"
CHECKED_IN_PACKAGE_KIND = "checked-in"

_REQUESTED_KEYS = {"config_version", "name", "env_id", "agent", "fixture",
                   "faults", "rules", "limits", "repetitions"}
_REQUESTED_KEYS_BACKEND = _REQUESTED_KEYS | {"backend"}
_BACKEND_KEYS = {"name"}
_BACKEND_KEYS_PACKAGE = {"name", "package"}
_PACKAGE_KEYS = {"kind", "identity"}
_FIXTURE_KEYS = {"order_id", "customer_id", "paid_minor", "currency"}
_FAULT_KEYS = {"hook", "params", "required"}
_FAULT_KEYS_TRIGGERED = {"hook", "params", "required", "trigger"}
_TRIGGER_KEYS = {"type", "tool", "occurrence"}
_LIMITS_KEYS = {"max_tool_calls", "max_total_tool_calls", "max_wall_clock_ms"}
_REPETITIONS_KEYS = {"count"}
_POLICY_KEYS = {"policy_version", "ceiling_tool_calls",
                "ceiling_total_tool_calls", "ceiling_repetitions",
                "ceiling_wall_clock_ms", "allowed_agents", "allowed_hooks"}
_RESOLVED_KEYS = {"schema_version", "resolved_config_id", "project_id",
                  "requested_config_id", "requested_digest", "resolved_at",
                  "resolved_by", "pins", "effective", "provenance", "digest"}
_PINS_KEYS = {"env_id", "env_version", "adapter_ref", "code_digest",
              "manifest_digest", "world_version", "adapter_version",
              "controller_version", "discovered_tools", "discovered_modes",
              "backend"}
# Resolved configs written before backend pinning carry no "backend" pin;
# they stay readable and execute as trusted-local.
_PINS_KEYS_LEGACY = _PINS_KEYS - {"backend"}
_PIN_BACKEND_KEYS = {"name", "backend_version"}
_PIN_BACKEND_KEYS_PACKAGE = {"name", "backend_version", "package_kind",
                             "package_identity"}
_EFFECTIVE_KEYS = {"agent", "fixture", "faults", "rules", "limits",
                   "repetitions", "seeds"}
_PROVENANCE_KEYS = {"policy_version", "requested_by", "requested_digest",
                    "resolved_by", "discovery_match"}

# Configs carry no secrets. Any secret-like marker in any string field is
# rejected, and the value is never echoed back in the error.
_SECRET_MARKERS = ("API_KEY", "APIKEY", "PRIVATE KEY", "client_secret",
                   "BEARER ", "xox-")


class ConfigError(ValueError):
    """A config is unsupported, invalid or an impossible combination."""


def canonical_digest(obj) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def scan_for_secrets(node, path: str = "config") -> None:
    """Reject secret-like text anywhere in a config object (pure)."""
    if isinstance(node, str):
        for marker in _SECRET_MARKERS:
            if marker in node:
                raise ConfigError(
                    f"{path} must not carry secret-like text"
                    f" (marker {marker.strip()!r}); configs hold no secrets")
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


def _check_short_string(value, path: str, limit: int = 100) -> str:
    if type(value) is not str or not value or len(value) > limit:
        raise ConfigError(f"invalid {path}")
    return value


def validate_policy(obj) -> dict:
    """Validate a controller-owned policy ceiling object (pure)."""
    if type(obj) is not dict or set(obj) != _POLICY_KEYS:
        raise ConfigError("policy must carry exactly the v1 policy fields")
    version = obj["policy_version"]
    if type(version) is not str or not POLICY_VERSION_PATTERN.match(version):
        raise ConfigError("invalid policy_version")
    ceilings = {}
    for key, low, high in (
            ("ceiling_tool_calls", MIN_TOOL_CALLS_PER_RUN,
             MAX_TOOL_CALLS_PER_RUN),
            ("ceiling_total_tool_calls", MIN_TOTAL_TOOL_CALLS,
             MAX_TOTAL_TOOL_CALLS),
            ("ceiling_repetitions", MIN_REPETITIONS, MAX_REPETITIONS),
            ("ceiling_wall_clock_ms", MIN_WALL_CLOCK_MS, MAX_WALL_CLOCK_MS)):
        value = obj[key]
        if type(value) is not int or not low <= value <= high:
            raise ConfigError(f"invalid {key}")
        ceilings[key] = value
    agents = obj["allowed_agents"]
    if (type(agents) is not list or not 1 <= len(agents) <= len(AGENTS)
            or any(a not in AGENTS for a in agents)
            or len(set(agents)) != len(agents)):
        raise ConfigError("allowed_agents must list known agents")
    hooks = obj["allowed_hooks"]
    if (type(hooks) is not list or len(hooks) > MAX_HOOKS
            or any(type(h) is not str or not HOOK_PATTERN.match(h)
                   for h in hooks)
            or len(set(hooks)) != len(hooks)):
        raise ConfigError("allowed_hooks must list 0-16 unique hook names")
    scan_for_secrets(obj, "policy")
    return {"policy_version": version, **ceilings,
            "allowed_agents": list(agents), "allowed_hooks": list(hooks)}


def _validate_refund_fixture(obj) -> dict:
    if type(obj) is not dict or set(obj) != _FIXTURE_KEYS:
        raise ConfigError("fixture must carry exactly order, customer,"
                          " paid_minor and currency")
    order_id = _check_short_string(obj["order_id"], "fixture.order_id")
    customer_id = _check_short_string(obj["customer_id"],
                                      "fixture.customer_id")
    currency = _check_short_string(obj["currency"], "fixture.currency")
    paid = obj["paid_minor"]
    if type(paid) is not int or not 0 < paid <= 10**12:
        raise ConfigError("invalid fixture.paid_minor")
    return {"order_id": order_id, "customer_id": customer_id,
            "paid_minor": paid, "currency": currency}


def _validate_fixture(obj, env_id: str) -> dict:
    from . import drivers as _drivers
    from . import drivers_refund as _refund_driver  # noqa: F401
    driver = _drivers.get_driver(env_id)
    validate = getattr(driver, "validate_fixture", None) \
        if driver is not None else None
    if not callable(validate):
        return _validate_refund_fixture(obj)
    try:
        validated = driver.validate_fixture(obj)
    except ConfigError:
        raise
    except (ValueError, TypeError, KeyError) as error:
        raise ConfigError(f"invalid fixture for env {env_id!r}:"
                          f" {error}") from error
    if type(validated) is not dict:
        raise ConfigError(f"invalid fixture for env {env_id!r}")
    return validated


def _validate_trigger(obj, path: str) -> dict:
    if type(obj) is not dict or set(obj) != _TRIGGER_KEYS:
        raise ConfigError(f"{path}.trigger must carry exactly type, tool"
                          " and occurrence")
    trigger_type = obj["type"]
    if trigger_type != "occurrence":
        raise ConfigError(
            f"{path}.trigger.type {trigger_type!r} is unsupported: v1"
            " triggers are 'occurrence' only")
    tool = obj["tool"]
    if tool != "*" and (type(tool) is not str
                        or not HOOK_PATTERN.match(tool)):
        raise ConfigError(f"invalid {path}.trigger.tool: '*' or a tool name")
    occurrence = obj["occurrence"]
    if type(occurrence) is not int \
            or not MIN_TRIGGER_OCCURRENCE <= occurrence \
            <= MAX_TRIGGER_OCCURRENCE:
        raise ConfigError(f"{path}.trigger.occurrence must be"
                          f" {MIN_TRIGGER_OCCURRENCE}-{MAX_TRIGGER_OCCURRENCE}")
    return {"type": "occurrence", "tool": tool, "occurrence": occurrence}


def _validate_faults(obj, allowed_hooks: list) -> list:
    if type(obj) is not list or len(obj) > MAX_FAULTS:
        raise ConfigError("faults must list 0-8 fault entries")
    seen = set()
    faults = []
    for index, entry in enumerate(obj):
        path = f"faults[{index}]"
        if type(entry) is not dict \
                or set(entry) not in (_FAULT_KEYS, _FAULT_KEYS_TRIGGERED):
            raise ConfigError(f"{path} must carry exactly hook, params"
                              " and required, plus optional trigger")
        hook = entry["hook"]
        if type(hook) is not str or not HOOK_PATTERN.match(hook):
            raise ConfigError(f"invalid {path}.hook")
        if hook in seen:
            raise ConfigError(f"duplicate fault hook {hook!r}")
        seen.add(hook)
        if hook not in allowed_hooks:
            raise ConfigError(f"unsupported fault hook {hook!r}")
        if type(entry["params"]) is not dict or entry["params"] != {}:
            raise ConfigError(f"{path}.params: v1 faults take no parameters")
        if type(entry["required"]) is not bool:
            raise ConfigError(f"{path}.required must be boolean")
        fault = {"hook": hook, "params": {},
                 "required": entry["required"]}
        if "trigger" in entry:
            fault["trigger"] = _validate_trigger(entry["trigger"], path)
        faults.append(fault)
    return faults


def _validate_rules(obj, env_id: str) -> list:
    from . import drivers as _drivers
    from . import drivers_refund as _refund_driver  # noqa: F401
    driver = _drivers.get_driver(env_id)
    allowed = list(driver.declared_checks()) if driver is not None \
        else list(INVARIANTS)
    if (type(obj) is not list or not 1 <= len(obj) <= len(allowed)
            or any(r not in allowed for r in obj)
            or len(set(obj)) != len(obj)):
        raise ConfigError("rules must list 1-"
                          f"{len(allowed)} unique known invariants:"
                          f" {', '.join(allowed)}")
    return list(obj)


def _validate_limits(obj, policy: dict) -> dict:
    if type(obj) is not dict or set(obj) != _LIMITS_KEYS:
        raise ConfigError("limits must carry exactly max_tool_calls,"
                          " max_total_tool_calls and max_wall_clock_ms")
    per_run = obj["max_tool_calls"]
    if type(per_run) is not int or not MIN_TOOL_CALLS_PER_RUN <= per_run \
            <= MAX_TOOL_CALLS_PER_RUN:
        raise ConfigError("max_tool_calls must be 2-1000: a refund attempt"
                          " needs at least get_order + issue_refund")
    total = obj["max_total_tool_calls"]
    if type(total) is not int or not MIN_TOTAL_TOOL_CALLS <= total \
            <= MAX_TOTAL_TOOL_CALLS:
        raise ConfigError("max_total_tool_calls must be 2-25000")
    wall = obj["max_wall_clock_ms"]
    if type(wall) is not int or not MIN_WALL_CLOCK_MS <= wall \
            <= MAX_WALL_CLOCK_MS:
        raise ConfigError("max_wall_clock_ms out of range")
    for key, ceiling_key in (("max_tool_calls", "ceiling_tool_calls"),
                             ("max_total_tool_calls",
                              "ceiling_total_tool_calls"),
                             ("max_wall_clock_ms", "ceiling_wall_clock_ms")):
        if obj[key] > policy[ceiling_key]:
            raise ConfigError(f"{key}={obj[key]} exceeds policy ceiling"
                              f" {ceiling_key}={policy[ceiling_key]};"
                              " ceilings are never raised or clamped")
    return {"max_tool_calls": per_run, "max_total_tool_calls": total,
            "max_wall_clock_ms": wall}


def _validate_repetitions(obj, policy: dict) -> dict:
    if type(obj) is not dict or set(obj) != _REPETITIONS_KEYS:
        raise ConfigError("repetitions must carry exactly count")
    count = obj["count"]
    if type(count) is not int or not MIN_REPETITIONS <= count \
            <= MAX_REPETITIONS:
        raise ConfigError("repetitions.count must be 1-25")
    if count > policy["ceiling_repetitions"]:
        raise ConfigError(f"repetitions.count={count} exceeds policy ceiling"
                          f" ceiling_repetitions={policy['ceiling_repetitions']}")
    return {"count": count}


def _validate_backend(obj) -> dict | None:
    """Validate the optional requested backend selector (pure, shape only).

    Returns the normalized ``{"name", "package"?}`` selector. Unknown
    fields are rejected; name validity is decided at resolve time, never
    here.
    """
    if type(obj) is not dict \
            or set(obj) not in (_BACKEND_KEYS, _BACKEND_KEYS_PACKAGE):
        raise ConfigError("backend must carry exactly name"
                          " and optional package")
    name = obj["name"]
    if type(name) is not str or not BACKEND_NAME_PATTERN.match(name):
        raise ConfigError("invalid backend.name")
    if "package" not in obj:
        return {"name": name}
    package = obj["package"]
    if type(package) is not dict or set(package) != _PACKAGE_KEYS:
        raise ConfigError(
            "backend.package must carry exactly kind and identity")
    kind = package["kind"]
    if type(kind) is not str or not BACKEND_NAME_PATTERN.match(kind):
        raise ConfigError("invalid backend.package.kind")
    identity = package["identity"]
    if type(identity) is not str or not identity or len(identity) > 256:
        raise ConfigError("invalid backend.package.identity")
    return {"name": name,
            "package": {"kind": kind, "identity": identity}}


def validate_backend_pin(obj) -> dict:
    """Validate a resolved backend pin (pure).

    A pin carries ``name`` + ``backend_version`` plus the package pair
    (``package_kind`` + ``package_identity``) or neither; anything else
    is rejected.
    """
    if type(obj) is not dict \
            or set(obj) not in (_PIN_BACKEND_KEYS,
                                _PIN_BACKEND_KEYS_PACKAGE):
        raise ConfigError("resolved backend pin must carry name and"
                          " backend_version, plus the package pair or"
                          " neither")
    name = obj["name"]
    if type(name) is not str or not BACKEND_NAME_PATTERN.match(name):
        raise ConfigError("invalid resolved backend name")
    version = obj["backend_version"]
    if type(version) is not str \
            or not BACKEND_VERSION_PATTERN.match(version):
        raise ConfigError("invalid resolved backend_version")
    pin = {"name": name, "backend_version": version}
    if "package_kind" in obj:
        kind = obj["package_kind"]
        identity = obj["package_identity"]
        if type(kind) is not str or not BACKEND_NAME_PATTERN.match(kind):
            raise ConfigError("invalid resolved package_kind")
        if type(identity) is not str or not identity \
                or len(identity) > 256:
            raise ConfigError("invalid resolved package_identity")
        pin["package_kind"] = kind
        pin["package_identity"] = identity
    return pin


def validate_requested(obj, policy_obj) -> dict:
    """Validate reusable client-supplied experiment settings (pure).

    Unknown fields are rejected at every level; nothing is silently ignored.
    Impossible combinations (per-run floor, aggregate coverage) and anything
    above policy ceilings are rejected, never adjusted.
    """
    policy = validate_policy(policy_obj)
    if type(obj) is not dict \
            or set(obj) not in (_REQUESTED_KEYS, _REQUESTED_KEYS_BACKEND):
        raise ConfigError("requested config must carry exactly the v1 fields:"
                          f" {', '.join(sorted(_REQUESTED_KEYS))},"
                          " plus optional backend")
    if type(obj["config_version"]) is not int \
            or obj["config_version"] != CONFIG_VERSION:
        raise ConfigError("unsupported config_version")
    name = _check_short_string(obj["name"], "name")
    env_id = obj["env_id"]
    if type(env_id) is not str or not ENV_ID_PATTERN.match(env_id):
        raise ConfigError("invalid env_id")
    agent = obj["agent"]
    if agent not in AGENTS:
        raise ConfigError(f"unsupported agent {agent!r}")
    if agent not in policy["allowed_agents"]:
        raise ConfigError(f"agent {agent!r} is not allowed by policy")
    fixture = _validate_fixture(obj["fixture"], env_id)
    faults = _validate_faults(obj["faults"], policy["allowed_hooks"])
    rules = _validate_rules(obj["rules"], env_id)
    limits = _validate_limits(obj["limits"], policy)
    repetitions = _validate_repetitions(obj["repetitions"], policy)
    if limits["max_tool_calls"] > limits["max_total_tool_calls"]:
        raise ConfigError(
            f"impossible combination: max_tool_calls="
            f"{limits['max_tool_calls']} exceeds max_total_tool_calls="
            f"{limits['max_total_tool_calls']}; the per-run budget can never"
            " fit the aggregate envelope")
    if repetitions["count"] * MIN_TOOL_CALLS_PER_RUN \
            > limits["max_total_tool_calls"]:
        raise ConfigError(
            f"impossible combination: {repetitions['count']} repetitions at"
            f" the {MIN_TOOL_CALLS_PER_RUN}-call minimum"
            f" ({repetitions['count'] * MIN_TOOL_CALLS_PER_RUN} calls) can"
            " never fit in max_total_tool_calls="
            f"{limits['max_total_tool_calls']}")
    backend = _validate_backend(obj["backend"]) if "backend" in obj \
        else None
    body = {"config_version": CONFIG_VERSION, "name": name, "env_id": env_id,
            "agent": agent, "fixture": fixture, "faults": faults,
            "rules": rules, "limits": limits, "repetitions": repetitions}
    if backend is not None:
        body["backend"] = backend
    scan_for_secrets(body, "requested config")
    return body


@dataclass(frozen=True)
class FixtureSpec:
    # Sorted ((key, value), ...) pairs from driver-validated fixture fields.
    # The shape is env-specific (refund keys, booking keys, ...); validation
    # happens in _validate_fixture before construction. to_dict round-trips
    # the validated mapping exactly, so digests are unchanged for refunds.
    values: tuple

    def to_dict(self) -> dict:
        return dict(self.values)


@dataclass(frozen=True)
class TriggerSpec:
    type: str
    tool: str
    occurrence: int

    def to_dict(self) -> dict:
        return {"type": self.type, "tool": self.tool,
                "occurrence": self.occurrence}


@dataclass(frozen=True)
class FaultSpec:
    hook: str
    required: bool
    trigger: TriggerSpec | None = None

    def to_dict(self) -> dict:
        body = {"hook": self.hook, "params": {},
                "required": self.required}
        if self.trigger is not None:
            body["trigger"] = self.trigger.to_dict()
        return body


@dataclass(frozen=True)
class LimitSpec:
    max_tool_calls: int
    max_total_tool_calls: int
    max_wall_clock_ms: int

    def to_dict(self) -> dict:
        return {"max_tool_calls": self.max_tool_calls,
                "max_total_tool_calls": self.max_total_tool_calls,
                "max_wall_clock_ms": self.max_wall_clock_ms}


@dataclass(frozen=True)
class RepetitionSpec:
    count: int

    def to_dict(self) -> dict:
        return {"count": self.count}


@dataclass(frozen=True)
class PackageSpec:
    kind: str
    identity: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "identity": self.identity}


@dataclass(frozen=True)
class BackendSpec:
    name: str
    package: PackageSpec | None = None

    def to_dict(self) -> dict:
        body = {"name": self.name}
        if self.package is not None:
            body["package"] = self.package.to_dict()
        return body


@dataclass(frozen=True)
class BackendPin:
    name: str
    backend_version: str
    package_kind: str | None = None
    package_identity: str | None = None

    def to_dict(self) -> dict:
        body = {"name": self.name, "backend_version": self.backend_version}
        if self.package_kind is not None:
            body["package_kind"] = self.package_kind
            body["package_identity"] = self.package_identity
        return body


@dataclass(frozen=True)
class Policy:
    """Immutable validated policy ceilings."""

    policy_version: str
    ceiling_tool_calls: int
    ceiling_total_tool_calls: int
    ceiling_repetitions: int
    ceiling_wall_clock_ms: int
    allowed_agents: tuple
    allowed_hooks: tuple

    @classmethod
    def from_obj(cls, obj) -> "Policy":
        body = validate_policy(obj)
        return cls(body["policy_version"], body["ceiling_tool_calls"],
                   body["ceiling_total_tool_calls"],
                   body["ceiling_repetitions"], body["ceiling_wall_clock_ms"],
                   tuple(body["allowed_agents"]),
                   tuple(body["allowed_hooks"]))

    def to_dict(self) -> dict:
        return {"policy_version": self.policy_version,
                "ceiling_tool_calls": self.ceiling_tool_calls,
                "ceiling_total_tool_calls": self.ceiling_total_tool_calls,
                "ceiling_repetitions": self.ceiling_repetitions,
                "ceiling_wall_clock_ms": self.ceiling_wall_clock_ms,
                "allowed_agents": list(self.allowed_agents),
                "allowed_hooks": list(self.allowed_hooks)}


@dataclass(frozen=True)
class RequestedConfig:
    """Immutable validated requested config (no identity or project yet)."""

    name: str
    env_id: str
    agent: str
    fixture: FixtureSpec
    faults: tuple
    rules: tuple
    limits: LimitSpec
    repetitions: RepetitionSpec
    backend: BackendSpec | None = None

    @classmethod
    def from_obj(cls, obj, policy_obj) -> "RequestedConfig":
        body = validate_requested(obj, policy_obj)
        fixture = FixtureSpec(values=tuple(sorted(body["fixture"].items())))
        faults = tuple(
            FaultSpec(f["hook"], f["required"],
                      TriggerSpec(**f["trigger"])
                      if "trigger" in f else None)
            for f in body["faults"])
        backend = None
        if "backend" in body:
            package = None
            if "package" in body["backend"]:
                package = PackageSpec(**body["backend"]["package"])
            backend = BackendSpec(body["backend"]["name"], package)
        return cls(body["name"], body["env_id"], body["agent"], fixture,
                   faults, tuple(body["rules"]),
                   LimitSpec(**body["limits"]),
                   RepetitionSpec(**body["repetitions"]), backend)

    def to_dict(self) -> dict:
        body = {"config_version": CONFIG_VERSION, "name": self.name,
                "env_id": self.env_id, "agent": self.agent,
                "fixture": self.fixture.to_dict(),
                "faults": [f.to_dict() for f in self.faults],
                "rules": list(self.rules),
                "limits": self.limits.to_dict(),
                "repetitions": self.repetitions.to_dict()}
        if self.backend is not None:
            body["backend"] = self.backend.to_dict()
        return body

    def digest(self) -> str:
        return canonical_digest(self.to_dict())


@dataclass(frozen=True)
class Pins:
    env_id: str
    env_version: str
    adapter_ref: str
    code_digest: str
    manifest_digest: str | None
    world_version: str
    adapter_version: str
    controller_version: str
    discovered_tools: tuple
    discovered_modes: tuple
    backend: BackendPin

    def to_dict(self) -> dict:
        return {"env_id": self.env_id, "env_version": self.env_version,
                "adapter_ref": self.adapter_ref,
                "code_digest": self.code_digest,
                "manifest_digest": self.manifest_digest,
                "world_version": self.world_version,
                "adapter_version": self.adapter_version,
                "controller_version": self.controller_version,
                "discovered_tools": [
                    {"name": name, "parameters": list(params)}
                    for name, params in self.discovered_tools],
                "discovered_modes": list(self.discovered_modes),
                "backend": self.backend.to_dict()}


@dataclass(frozen=True)
class Provenance:
    policy_version: str
    requested_by: str
    requested_digest: str
    resolved_by: str
    discovery_match: bool

    def to_dict(self) -> dict:
        return {"policy_version": self.policy_version,
                "requested_by": self.requested_by,
                "requested_digest": self.requested_digest,
                "resolved_by": self.resolved_by,
                "discovery_match": self.discovery_match}


@dataclass(frozen=True)
class Effective:
    agent: str
    fixture: FixtureSpec
    faults: tuple
    rules: tuple
    limits: LimitSpec
    repetitions: RepetitionSpec
    seeds: tuple

    def to_dict(self) -> dict:
        return {"agent": self.agent, "fixture": self.fixture.to_dict(),
                "faults": [f.to_dict() for f in self.faults],
                "rules": list(self.rules),
                "limits": self.limits.to_dict(),
                "repetitions": self.repetitions.to_dict(),
                "seeds": list(self.seeds)}


@dataclass(frozen=True)
class ResolvedConfig:
    """Immutable controller-issued binding of settings to pinned versions."""

    resolved_config_id: str
    project_id: str
    requested_config_id: str
    requested_digest: str
    resolved_at: str
    resolved_by: str
    pins: Pins
    effective: Effective
    provenance: Provenance
    digest: str

    def body(self) -> dict:
        return {"schema_version": EXPERIMENTS_SCHEMA_VERSION,
                "resolved_config_id": self.resolved_config_id,
                "project_id": self.project_id,
                "requested_config_id": self.requested_config_id,
                "requested_digest": self.requested_digest,
                "resolved_at": self.resolved_at,
                "resolved_by": self.resolved_by,
                "pins": self.pins.to_dict(),
                "effective": self.effective.to_dict(),
                "provenance": self.provenance.to_dict(),
                "digest": self.digest}

    def verify(self) -> None:
        body = self.body()
        digest = body.pop("digest")
        if canonical_digest(body) != digest:
            raise ConfigError("resolved config digest mismatch")


def requested_config_id(project_id: str, body: dict) -> str:
    """Content-addressed id, so identical creates are idempotent per project."""
    return "req-" + canonical_digest(
        {"project_id": project_id, "config": body})[:16]


def resolved_config_id(project_id: str, requested_id: str, stable: dict) -> str:
    return "res-" + canonical_digest(
        {"project_id": project_id, "requested_config_id": requested_id,
         "resolved": stable})[:16]


def _required_tools(agent: str) -> tuple:
    if agent == "vulnerable" or agent.endswith("-noread"):
        return ("get_order", "issue_refund")
    return ("get_order", "issue_refund", "get_refund_status")


def _backend_pin_for(requested: RequestedConfig, registration,
                     backends) -> BackendPin:
    """Pin the requested backend without probing any backend.

    ``backends`` maps known backend names to backend instances (or version
    strings); None consults the controller's registry. ``trusted-local``
    is always known. Unknown names are rejected; availability is never
    probed here (it is an execution-time refusal).
    """
    name = requested.backend.name if requested.backend is not None \
        else TRUSTED_LOCAL_BACKEND
    if name == TRUSTED_LOCAL_BACKEND:
        return BackendPin(name, TRUSTED_LOCAL_VERSION)
    version = None
    if backends is None:
        from .controller import get_backend
        try:
            backend = get_backend(name)
        except ConfigError:
            raise ConfigError(
                f"unknown backend {name!r}; register the backend before"
                " resolving") from None
        version = getattr(backend, "version", None)
    else:
        if name not in backends:
            raise ConfigError(
                f"unknown backend {name!r}; register the backend before"
                " resolving")
        candidate = backends[name]
        version = candidate if isinstance(candidate, str) \
            else getattr(candidate, "version", None)
    if type(version) is not str or not BACKEND_VERSION_PATTERN.match(version):
        raise ConfigError(
            f"backend {name!r} reports an invalid version")
    if requested.backend is not None and requested.backend.package is not None:
        package = requested.backend.package
        return BackendPin(name, version, package.kind, package.identity)
    return BackendPin(name, version, CHECKED_IN_PACKAGE_KIND,
                      registration["code_digest"])


def resolve_requested(requested: RequestedConfig, *, project_id: str,
                      requested_id: str, requested_digest: str,
                      requested_by: str, policy: Policy, registry,
                      manifest_digest: str | None = None,
                      resolved_by: str = "controller",
                      backends=None) -> ResolvedConfig:
    """Bind a requested config to pinned versions (describe-only env access).

    Reads the environment registration and runs capability discovery, which
    instantiates the adapter and calls ``describe()`` only. It never resets,
    invokes tools, arms faults or otherwise mutates the target environment,
    and never calls a model. Declaration mismatches, undiscovered fault hooks
    and missing required tools are rejected as impossible combinations, and
    rules outside the env driver's declared checks are rejected as
    unsupported (envs without a registered driver skip that check).

    Backend pinning resolves the requested backend name against ``backends``
    (a name-to-backend mapping) or, when None, the controller's registry;
    unknown names are rejected. Resolution never probes backend
    availability. A requested package is pinned as given, otherwise the
    registered adapter's code digest is pinned as a ``checked-in`` package.
    """
    check_project_id(project_id)
    if type(requested_id) is not str or not requested_id:
        raise ConfigError("invalid requested_config_id")
    if type(requested_digest) is not str or not requested_digest:
        raise ConfigError("invalid requested_digest")
    if type(requested_by) is not str or not requested_by \
            or len(requested_by) > 200:
        raise ConfigError("invalid requested_by")
    if type(resolved_by) is not str or not resolved_by \
            or len(resolved_by) > 200:
        raise ConfigError("invalid resolved_by")
    try:
        registration = registry.get(requested.env_id)
    except ValueError as error:
        raise ConfigError(f"unknown env_id {requested.env_id!r}") from error
    try:
        found = discover(registry, requested.env_id,
                         manifest_digest=manifest_digest)
    except ValueError as error:
        raise ConfigError(f"capability discovery failed: {error}") from error
    if not found["match"]:
        raise ConfigError("env declaration mismatch:"
                          f" {', '.join(found['mismatches'])};"
                          " re-register or re-qualify before resolving")
    discovered_hooks = found["fault_hooks"]
    for fault in requested.faults:
        if fault.hook not in discovered_hooks:
            raise ConfigError(
                f"impossible combination: fault hook {fault.hook!r} is not"
                f" implemented by env {requested.env_id!r}")
    from . import drivers as _drivers
    from . import drivers_refund as _refund_driver  # noqa: F401
    driver = _drivers.get_driver(requested.env_id)
    if driver is not None:
        declared = list(driver.declared_checks())
        for rule in requested.rules:
            if rule not in declared:
                raise ConfigError(
                    f"unsupported check {rule!r} for env"
                    f" {requested.env_id!r}: the env declares"
                    f" {', '.join(declared)}")
    if driver is not None:
        if requested.agent not in driver.agents():
            raise ConfigError(
                f"unsupported agent {requested.agent!r} for env"
                f" {requested.env_id!r}: the env driver implements"
                f" {', '.join(driver.agents())}")
        required = list(driver.required_tools(requested.agent))
    else:
        required = list(_required_tools(requested.agent))
    discovered_names = {t["name"] for t in found["discovered_tools"]}
    missing = [t for t in required if t not in discovered_names]
    if missing:
        raise ConfigError("impossible combination: agent"
                          f" {requested.agent!r} needs tool(s)"
                          f" {', '.join(missing)}, which env"
                          f" {requested.env_id!r} does not implement")
    for fault in requested.faults:
        trigger = fault.trigger
        if trigger is not None and trigger.tool != "*" \
                and trigger.tool not in discovered_names:
            raise ConfigError(
                f"impossible combination: fault trigger tool"
                f" {trigger.tool!r} is not implemented by env"
                f" {requested.env_id!r}")
    backend_pin = _backend_pin_for(requested, registration, backends)
    pins = Pins(
        env_id=requested.env_id,
        env_version=registration["env_version"],
        adapter_ref=registration["adapter_ref"],
        code_digest=registration["code_digest"],
        manifest_digest=manifest_digest,
        world_version=WORLD_VERSION,
        adapter_version=ADAPTER_VERSION,
        controller_version=CONTROLLER_VERSION,
        discovered_tools=tuple(
            (t["name"], tuple(t["parameters"]))
            for t in found["discovered_tools"]),
        discovered_modes=tuple(found["discovered_modes"]),
        backend=backend_pin)
    effective = Effective(
        agent=requested.agent, fixture=requested.fixture,
        faults=requested.faults, rules=requested.rules,
        limits=requested.limits, repetitions=requested.repetitions,
        seeds=tuple(range(requested.repetitions.count)))
    provenance = Provenance(
        policy_version=policy.policy_version, requested_by=requested_by,
        requested_digest=requested_digest, resolved_by=resolved_by,
        discovery_match=True)
    stable = {"pins": pins.to_dict(), "effective": effective.to_dict(),
              "provenance": provenance.to_dict()}
    resolved_id = resolved_config_id(project_id, requested_id, stable)
    resolved_at = utcnow()
    digest = canonical_digest(
        {"schema_version": EXPERIMENTS_SCHEMA_VERSION,
         "resolved_config_id": resolved_id, "project_id": project_id,
         "requested_config_id": requested_id,
         "requested_digest": requested_digest, "resolved_at": resolved_at,
         "resolved_by": resolved_by, **deepcopy(stable)})
    return ResolvedConfig(resolved_id, project_id, requested_id,
                          requested_digest, resolved_at, resolved_by,
                          pins, effective, provenance, digest)


def resolved_from_dict(obj) -> dict:
    """Validate a stored resolved body and verify its digest (pure)."""
    if type(obj) is not dict or set(obj) != _RESOLVED_KEYS:
        raise ConfigError("resolved config must carry exactly the v1 fields")
    if obj["schema_version"] != EXPERIMENTS_SCHEMA_VERSION:
        raise ConfigError("unsupported resolved schema_version")
    for key in ("resolved_config_id", "project_id", "requested_config_id",
                "requested_digest", "resolved_at", "resolved_by"):
        if type(obj[key]) is not str or not obj[key]:
            raise ConfigError(f"invalid resolved {key}")
    if type(obj["pins"]) is not dict \
            or set(obj["pins"]) not in (_PINS_KEYS, _PINS_KEYS_LEGACY):
        raise ConfigError("invalid resolved pins")
    if "backend" in obj["pins"]:
        validate_backend_pin(obj["pins"]["backend"])
    if type(obj["effective"]) is not dict \
            or set(obj["effective"]) != _EFFECTIVE_KEYS:
        raise ConfigError("invalid resolved effective values")
    if type(obj["provenance"]) is not dict \
            or set(obj["provenance"]) != _PROVENANCE_KEYS:
        raise ConfigError("invalid resolved provenance")
    body = {k: v for k, v in obj.items() if k != "digest"}
    if canonical_digest(body) != obj["digest"]:
        raise ConfigError("resolved config digest mismatch")
    return deepcopy(obj)
