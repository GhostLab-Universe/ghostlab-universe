"""Environment adapter interface (``ghostlab-extensions/1``): the frozen world side.

A reviewed local adapter package implements :class:`EnvironmentAdapter` for
one world version. The controller and the generic runner treat the adapter as
the authoritative world: ``reset`` starts a clean run from a fixture snapshot
and a fault schedule, ``execute`` applies exactly one already-validated
action, ``observe`` returns authoritative state and evidence, and
``evaluate`` derives deterministic invariant checks from that state.

The controller validates every action (allowlisted tool, schema, budget)
before dispatch. The adapter records every applied action in its trace and
owns only the world's own checks (identity, existence, ceilings). Standard
library only: replay and the deterministic core stay decoupled from model,
service and ORM libraries.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

EXTENSION_INTERFACE_VERSION = "ghostlab-extensions/1"

EFFECTS = frozenset({"read", "mutate", "deliver", "handoff"})
PARAMETER_TYPES = frozenset({"string", "integer", "boolean"})
STATUSES = frozenset({"ok", "error"})


class UnsupportedOperation(Exception):
    """An optional capability (e.g. snapshots) this adapter does not implement.

    The controller and the generic runner never assume optional capabilities:
    a world without them runs, verifies and replays exactly the same way.
    """


class ActionBudgetExceeded(Exception):
    """The environment refused an action past its own tool budget.

    The generic runner maps this to ``budget_exhausted``. The refusal records
    no trace event, mirroring a world call that raises before dispatch.
    """


def _text(value: Any, label: str, *, max_length: int = 200) -> str:
    if type(value) is not str or not value or len(value) > max_length:
        raise ValueError(f"Invalid {label}")
    return value


def _tuple(values: Any, label: str, *, max_length: int = 32) -> tuple:
    if isinstance(values, (list, tuple)) and len(values) <= max_length:
        return tuple(values)
    raise ValueError(f"Invalid {label}")


@dataclass(frozen=True)
class ToolSpec:
    """One tool the world offers, exactly as the reviewed package declares it."""

    name: str
    effect: str
    parameters: Mapping[str, Any]
    description: str = ""

    def __post_init__(self):
        _text(self.name, "tool name", max_length=64)
        if self.effect not in EFFECTS:
            raise ValueError(f"Invalid tool effect {self.effect!r}")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("Invalid tool parameters")
        if type(self.description) is not str or len(self.description) > 2000:
            raise ValueError("Invalid tool description")


@dataclass(frozen=True)
class FaultParameterSpec:
    """One declared parameter of an injectable fault."""

    name: str
    type: str
    description: str = ""

    def __post_init__(self):
        _text(self.name, "fault parameter name", max_length=64)
        if self.type not in PARAMETER_TYPES:
            raise ValueError(f"Invalid fault parameter type {self.type!r}")
        if type(self.description) is not str or len(self.description) > 2000:
            raise ValueError("Invalid fault parameter description")


@dataclass(frozen=True)
class FaultSpec:
    """One injectable fault: its parameters and the points where it can fire."""

    fault_id: str
    parameters: tuple[FaultParameterSpec, ...] = ()
    trigger_points: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self):
        _text(self.fault_id, "fault id", max_length=64)
        parameters = _tuple(self.parameters, "fault parameters", max_length=8)
        for parameter in parameters:
            if not isinstance(parameter, FaultParameterSpec):
                raise ValueError("Invalid fault parameter")
        object.__setattr__(self, "parameters", parameters)
        trigger_points = _tuple(self.trigger_points, "fault trigger points", max_length=8)
        for point in trigger_points:
            _text(point, "fault trigger point", max_length=100)
        object.__setattr__(self, "trigger_points", trigger_points)
        if type(self.description) is not str or len(self.description) > 2000:
            raise ValueError("Invalid fault description")


@dataclass(frozen=True)
class EnvironmentDescription:
    """Everything the controller needs to know about one world version."""

    world_version: str
    environment_id: str
    environment_version: str
    tools: tuple[ToolSpec, ...]
    faults: tuple[FaultSpec, ...]
    invariants: tuple[str, ...]
    fixtures: tuple[str, ...]
    capabilities: Mapping[str, bool]
    source: str

    def __post_init__(self):
        _text(self.world_version, "world version", max_length=100)
        _text(self.environment_id, "environment id", max_length=100)
        _text(self.environment_version, "environment version", max_length=100)
        tools = _tuple(self.tools, "tools", max_length=32)
        if not tools:
            raise ValueError("A world offers at least one tool")
        for tool in tools:
            if not isinstance(tool, ToolSpec):
                raise ValueError("Invalid tool")
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("Duplicate tool name")
        object.__setattr__(self, "tools", tools)
        faults = _tuple(self.faults, "faults", max_length=16)
        if not faults:
            raise ValueError("A world declares at least one fault")
        for fault in faults:
            if not isinstance(fault, FaultSpec):
                raise ValueError("Invalid fault")
        if len({fault.fault_id for fault in faults}) != len(faults):
            raise ValueError("Duplicate fault id")
        object.__setattr__(self, "faults", faults)
        invariants = _tuple(self.invariants, "invariants", max_length=16)
        if not invariants:
            raise ValueError("A world declares at least one invariant")
        for invariant in invariants:
            _text(invariant, "invariant name", max_length=100)
        if len(set(invariants)) != len(invariants):
            raise ValueError("Duplicate invariant name")
        object.__setattr__(self, "invariants", invariants)
        fixtures = _tuple(self.fixtures, "fixtures", max_length=32)
        if not fixtures:
            raise ValueError("A world ships at least one fixture")
        for fixture in fixtures:
            _text(fixture, "fixture name", max_length=100)
        if len(set(fixtures)) != len(fixtures):
            raise ValueError("Duplicate fixture name")
        object.__setattr__(self, "fixtures", fixtures)
        if not isinstance(self.capabilities, Mapping):
            raise ValueError("Invalid capabilities")
        for key, value in self.capabilities.items():
            if type(key) is not str or type(value) is not bool:
                raise ValueError("Invalid capabilities")
        if "snapshot_restore" not in self.capabilities:
            raise ValueError("Capabilities must declare snapshot_restore")
        _text(self.source, "adapter source", max_length=200)


@dataclass(frozen=True)
class Action:
    """One validated tool call the runner may dispatch into the environment."""

    tool: str
    arguments: Mapping[str, Any]

    def __post_init__(self):
        _text(self.tool, "action tool", max_length=100)
        if not isinstance(self.arguments, Mapping):
            raise ValueError("Invalid action arguments")
        for key in self.arguments:
            if type(key) is not str:
                raise ValueError("Invalid action argument name")


@dataclass(frozen=True)
class Observation:
    """Authoritative evidence so far: the trace, the state and the call count.

    Adapters return private copies; the runner and targets treat the contents
    as read-only. Mutating them corrupts only the reader's own view.
    """

    trace: list
    state: Mapping[str, Any]
    calls: int

    def __post_init__(self):
        if type(self.trace) is not list or len(self.trace) > 1000:
            raise ValueError("Invalid trace")
        for event in self.trace:
            if not isinstance(event, Mapping):
                raise ValueError("Invalid trace event")
        if not isinstance(self.state, Mapping):
            raise ValueError("Invalid state")
        if type(self.calls) is not int or self.calls < 0 or self.calls > 1000:
            raise ValueError("Invalid call count")


@dataclass(frozen=True)
class Outcome:
    """The recorded result of one applied action."""

    status: str
    event_id: int
    result: Mapping[str, Any] | None = None
    error: str | None = None
    message: str | None = None

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"Invalid outcome status {self.status!r}")
        if type(self.event_id) is not int or self.event_id < 1 or self.event_id > 1000:
            raise ValueError("Invalid outcome event id")
        if self.status == "ok":
            if not isinstance(self.result, Mapping):
                raise ValueError("ok outcomes carry a result")
            if self.error is not None or self.message is not None:
                raise ValueError("ok outcomes carry no error")
        else:
            if self.result is not None:
                raise ValueError("error outcomes carry no result")
            _text(self.error, "outcome error", max_length=100)
            if type(self.message) is not str:
                raise ValueError("Invalid outcome message")


@dataclass(frozen=True)
class InvariantCheck:
    """One deterministic invariant verdict from authoritative evidence."""

    name: str
    passed: bool
    evidence_event_ids: tuple[int, ...] = ()
    observed: int | None = None
    limit: int | None = None
    unit: str | None = None

    def __post_init__(self):
        _text(self.name, "invariant name", max_length=100)
        if type(self.passed) is not bool:
            raise ValueError("Invalid invariant verdict")
        event_ids = _tuple(self.evidence_event_ids, "evidence event ids", max_length=1000)
        for event_id in event_ids:
            if type(event_id) is not int or event_id < 1 or event_id > 1000:
                raise ValueError("Invalid evidence event id")
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("Duplicate evidence event id")
        object.__setattr__(self, "evidence_event_ids", event_ids)
        for value, label in ((self.observed, "observed"), (self.limit, "limit")):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"Invalid {label} value")
        if (self.observed is None) != (self.limit is None):
            raise ValueError("Observed and limit values come together")
        if self.observed is not None and self.unit is None:
            raise ValueError("Measured values need a unit")
        if self.unit is not None:
            _text(self.unit, "unit", max_length=100)
        if not self.passed and not event_ids:
            raise ValueError("A failed invariant must cite evidence events")


@runtime_checkable
class EnvironmentAdapter(Protocol):
    """The authoritative world behind one world version.

    One instance serves one run at a time: ``reset`` starts a clean run,
    ``execute``/``observe`` serve it, and ``close`` ends it. Adapters are
    synchronous and single-threaded; parallel runs use separate instances.
    """

    def describe(self) -> EnvironmentDescription:
        """Tools, effects, versions, faults, invariants, fixtures, capabilities."""
        ...

    def named_fixture(self, name: str) -> Mapping[str, Any]:
        """The fixture snapshot shipped under ``name``. Unknown names raise KeyError."""
        ...

    def reset(self, fixture: Mapping[str, Any], scenario: Mapping[str, Any], *,
              actor_id: str) -> Observation:
        """Start a clean run from a fixture snapshot and a fault schedule.

        ``fixture`` is a snapshot as described (see ``named_fixture``);
        ``scenario`` carries the fault schedule and the tool budget;
        ``actor_id`` is the controller-bound identity, never model text.
        Invalid input raises ValueError; nothing runs until ``reset`` succeeds.
        """
        ...

    def execute(self, action: Action) -> Outcome:
        """Apply exactly one validated action and return its recorded outcome.

        Every applied action appends one trace event, including world errors.
        Only a budget refusal raises (:class:`ActionBudgetExceeded`) instead of
        recording. Malformed actions raise before touching world state.
        """
        ...

    def observe(self) -> Observation:
        """Authoritative trace, state and call count. Private copies."""
        ...

    def evaluate(self, *, trace: list, state: Mapping[str, Any]) -> list[InvariantCheck]:
        """Deterministic invariant checks from authoritative evidence.

        A pure function of the given evidence: the same trace and state always
        produce the same checks, on a live adapter or a fresh one.
        """
        ...

    def faults_fired(self, scenario: Mapping[str, Any], trace: list) -> frozenset[str]:
        """Fault ids that demonstrably took effect in this trace.

        A fault counts only if it changed something the run could have acted
        on; a scheduled fault that never fired is not reported.
        """
        ...

    def close(self) -> None:
        """Release run resources. Idempotent; the runner always calls it."""
        ...

    def snapshot(self) -> Mapping[str, Any]:
        """Optional opaque run snapshot. Raises :class:`UnsupportedOperation`
        when the adapter does not implement snapshots. Never assumed."""
        ...

    def restore(self, snapshot: Mapping[str, Any]) -> Observation:
        """Optional restore of :meth:`snapshot`. Raises
        :class:`UnsupportedOperation` when the adapter does not implement
        snapshots, ValueError on corrupt input. Never assumed."""
        ...