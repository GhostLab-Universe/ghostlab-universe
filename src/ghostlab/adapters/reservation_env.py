"""Checked-in reservation environment adapter behind the frozen P1 interfaces.

:class:`ReservationEnvironment` drives the vendored external reservation
service (``reservation_service.py`` in this package, byte-identical to the
file of the same name at ``ghostlab-ext-example@79684eb``): a deterministic,
stdlib-only seat/inventory world with four tools and three fault hooks. The
adapter maps that world onto ``ghostlab-extensions/1`` exactly the way
:mod:`ghostlab.adapters.refund_env` maps the refund world: ``reset`` starts a
clean run from a named immutable fixture plus a fault schedule, ``execute``
applies exactly one validated action and records one trace event, ``observe``
returns authoritative evidence, ``evaluate`` derives invariant checks purely
from that evidence, and ``faults_fired`` reports only faults with
demonstrable effect.

Provenance and trust: this package is reviewed local code. The operator
registers it explicitly via :func:`register_reservation_adapters`; nothing is
imported by name from a proposal, capsule or request, no path is ever loaded
or executed, and the browser never runs uploaded Python. The vendored service
file changes only by re-vendoring a reviewed external revision (compare
:data:`VENDOR_SHA256`); behavior and fixtures stay developer-provided -- the
P2 OpenAPI importer contributes tool definitions only.

World specifics (the external README carries the full service contract):

* Fixtures: ``empty_small``, ``full``, ``half_full``, ``near_full``.
* Scenario keys: the three fault toggles plus ``max_tool_calls`` (1-1000).
* State is the service observation minus run-lifecycle keys (the ``faults``
  schedule and the ``closed`` flag): the schedule is run input, not evidence,
  so baseline and faulted runs share identical pre-divergence states.
* Identity: the reservation world is multi-holder (fixtures pin several
  holders), so ``actor_id`` is validated and recorded on every trace event
  but holders are not bound to it.
* Snapshots are honestly unsupported. The service exposes reset/observe but
  no snapshot API, so ``capabilities["snapshot_restore"]`` is False and both
  methods raise :class:`UnsupportedOperation`. Replay re-executes recorded
  actions instead.

Standard library only.
"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ghostlab.adapters.reservation_service import (ALLOWED_TOOLS, FAULTS, ReservationService,
                                                   TimeoutAfterCommit, get_fixture,
                                                   list_fixtures)
from ghostlab.extensions.environment import (Action, ActionBudgetExceeded, EnvironmentAdapter,
                                             EnvironmentDescription, FaultSpec, InvariantCheck,
                                             Observation, Outcome, ToolSpec, UnsupportedOperation)
from ghostlab.extensions.registry import AdapterManifest, AdapterRegistry

RESERVATION_WORLD_VERSION = "reservation-world/1"
ENVIRONMENT_ID = "reservation-environment"
ENVIRONMENT_VERSION = "reservation-environment/1"
SOURCE = "ghostlab.adapters.reservation_env (checked-in; drives the vendored reservation service)"
SERVICE_PROVENANCE = "ghostlab-ext-example@79684eb reservation_service.py"
VENDOR_SHA256 = "C54FB584D297A4B5D910B56ADD1C50FB6B12D53C34722493E42F89FBCB2D1464"

TIMEOUT_FAULT = "timeout_after_commit"
STALE_FAULT = "stale_snapshot"
SKIP_FAULT = "skip_capacity_check"
FAULT_IDS = (TIMEOUT_FAULT, STALE_FAULT, SKIP_FAULT)

RESERVATION_INVARIANTS = ("capacity_respected", "records_wellformed", "used_consistent",
                          "used_nonnegative")
RESERVATION_FIXTURES = tuple(sorted(list_fixtures()))

DEFAULT_MAX_TOOL_CALLS = 20
_ID = {"type": "string", "minLength": 1, "maxLength": 100}
_HOLDER = {"type": "string", "minLength": 1, "maxLength": 100}
_SEATS = {"type": "integer", "minimum": 1}


def _object(**fields: dict) -> dict:
    """Flat tool parameter schema, mirroring the reviewed world-pack style."""
    return {"type": "object", "additionalProperties": False, "required": sorted(fields),
            "properties": {name: dict(spec) for name, spec in sorted(fields.items())}}


RESERVATION_TOOL_SCHEMAS = {
    "check_availability": _object(),
    "reserve": _object(holder=_HOLDER, seats=_SEATS),
    "cancel": _object(reservation_id=_ID),
    "get_status": _object(reservation_id=_ID),
}

TOOL_EFFECTS = {
    "check_availability": "read",
    "reserve": "mutate",
    "cancel": "mutate",
    "get_status": "read",
}

TOOL_DESCRIPTIONS = {
    "check_availability": "Read a live capacity snapshot (used, available, revision)",
    "reserve": "Commit one reservation for a holder, within capacity",
    "cancel": "Cancel an active reservation, freeing its seats",
    "get_status": "Look up one reservation record by id",
}

FAULT_TRIGGER_POINTS = {
    TIMEOUT_FAULT: ("reserve_commit",),
    STALE_FAULT: ("check_availability",),
    SKIP_FAULT: ("reserve_commit",),
}

FAULT_DESCRIPTIONS = {
    TIMEOUT_FAULT: "A reserve commits, then its response times out",
    STALE_FAULT: "Availability reads return the last cached snapshot, flagged stale",
    SKIP_FAULT: "Reserve skips capacity enforcement (buggy commit)",
}

# Run-lifecycle keys dropped from adapter state (see the module docstring).
_NON_EVIDENCE_KEYS = frozenset({"faults", "closed"})


def revision_growth_events(trace: list) -> list[int]:
    """Event ids at which the authoritative revision grew.

    The single evidence rule shared by :meth:`ReservationEnvironment.evaluate`
    and every reader, so all of them cite the same events.
    """
    events, high = [], 0
    for event in trace:
        revision = event["state"]["revision"]
        if revision > high:
            events.append(event["event_id"])
            high = revision
    return events


def _describe() -> EnvironmentDescription:
    return EnvironmentDescription(
        world_version=RESERVATION_WORLD_VERSION, environment_id=ENVIRONMENT_ID,
        environment_version=ENVIRONMENT_VERSION,
        tools=[ToolSpec(name=name, effect=TOOL_EFFECTS[name],
                        parameters=deepcopy(RESERVATION_TOOL_SCHEMAS[name]),
                        description=TOOL_DESCRIPTIONS[name])
               for name in ALLOWED_TOOLS],
        faults=[FaultSpec(fault_id=fault_id, trigger_points=FAULT_TRIGGER_POINTS[fault_id],
                          description=FAULT_DESCRIPTIONS[fault_id])
                for fault_id in FAULT_IDS],
        invariants=list(RESERVATION_INVARIANTS), fixtures=list(RESERVATION_FIXTURES),
        capabilities={"snapshot_restore": False}, source=SOURCE)


def _parse_fixture(fixture: Mapping[str, Any]) -> str:
    """The fixture name, after proving the snapshot matches the shipped one."""
    if not isinstance(fixture, Mapping):
        raise ValueError("Invalid fixture or scenario")
    try:
        name = fixture["fixture"]
    except KeyError:
        raise ValueError("Fixture snapshot needs a 'fixture' name") from None
    if type(name) is not str or not name:
        raise ValueError(f"Invalid fixture name {name!r}")
    try:
        shipped = get_fixture(name)
    except KeyError:
        raise ValueError(f"Unknown reservation fixture {name!r}") from None
    if set(fixture) != {"fixture", "capacity", "reservations"}:
        raise ValueError("Fixture snapshot must be exactly fixture/capacity/reservations")
    if (fixture["capacity"] != shipped["capacity"]
            or fixture["reservations"] != shipped["reservations"]):
        raise ValueError("Fixture snapshot does not match the shipped fixture")
    return name


def _parse_scenario(scenario: Mapping[str, Any]) -> tuple[dict[str, bool], int]:
    """Fault toggles plus the tool budget; unknown keys and bad types refuse."""
    if not isinstance(scenario, Mapping):
        raise ValueError("Invalid fixture or scenario")
    unknown = set(scenario) - {*FAULT_IDS, "max_tool_calls"}
    if unknown:
        raise ValueError(f"Unknown scenario keys: {sorted(unknown, key=repr)}")
    faults = {}
    for fault_id in FAULT_IDS:
        enabled = scenario.get(fault_id, False)
        if type(enabled) is not bool:
            raise ValueError(f"Scenario fault {fault_id!r} must be boolean")
        faults[fault_id] = enabled
    cap = scenario.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)
    if type(cap) is not int or not 1 <= cap <= 1000:
        raise ValueError("max_tool_calls must be between 1 and 1000")
    return faults, cap


def _check_actor(actor_id: str) -> str:
    if type(actor_id) is not str or not actor_id or len(actor_id) > 100:
        raise ValueError("Invalid actor_id")
    return actor_id


def _evidence_state(service: ReservationService) -> dict:
    """Authoritative state minus run-lifecycle keys. A private copy."""
    return {key: value for key, value in service.observe().items()
            if key not in _NON_EVIDENCE_KEYS}


class ReservationEnvironment:
    """The external reservation service as an extension environment adapter.

    One instance serves one run: ``reset`` builds a fresh vendored service
    from a named fixture plus a fault schedule, ``execute`` dispatches exactly
    one action through ``call_tool`` (a commit-then-timeout becomes a recorded
    ``TimeoutAfterCommit`` error, exactly like the refund world's
    ``PaymentTimeout``), and ``close`` ends the run. The vendored service is
    never shared between instances, so parallel runs stay isolated.
    """

    def __init__(self):
        self._service: ReservationService | None = None
        self._trace: list = []
        self._calls = 0
        self._cap = DEFAULT_MAX_TOOL_CALLS
        self._actor = ""
        self._closed = False
        self._description = _describe()

    def describe(self) -> EnvironmentDescription:
        return self._description

    def named_fixture(self, name: str) -> Mapping[str, Any]:
        if type(name) is not str:
            raise KeyError(f"Unknown {RESERVATION_WORLD_VERSION} fixture {name!r}")
        try:
            shipped = get_fixture(name)
        except KeyError:
            raise KeyError(f"Unknown {RESERVATION_WORLD_VERSION} fixture {name!r}") from None
        return {"fixture": name, "capacity": shipped["capacity"],
                "reservations": shipped["reservations"]}

    def reset(self, fixture: Mapping[str, Any], scenario: Mapping[str, Any], *,
              actor_id: str) -> Observation:
        name = _parse_fixture(fixture)
        faults, cap = _parse_scenario(scenario)
        actor = _check_actor(actor_id)
        service = ReservationService(name)
        for fault_id, enabled in faults.items():
            if enabled:
                service.set_fault(fault_id, True)
        self._service = service
        self._trace = []
        self._calls = 0
        self._cap = cap
        self._actor = actor
        self._closed = False
        return self.observe()

    def _running(self) -> ReservationService:
        if self._service is None or self._closed:
            raise RuntimeError("Environment is not running (reset first)")
        return self._service

    def execute(self, action: Action) -> Outcome:
        service = self._running()
        if not isinstance(action, Action):
            raise TypeError("Environment actions must be Action")
        if self._calls >= self._cap:
            raise ActionBudgetExceeded("Tool call budget exhausted")
        self._calls += 1
        request = {"tool": action.tool, "arguments": deepcopy(dict(action.arguments))}
        try:
            reply = service.call_tool(action.tool, dict(action.arguments))
        except TimeoutAfterCommit as timeout:
            return self._record({"error": "TimeoutAfterCommit", "message": str(timeout)},
                                request, "TimeoutAfterCommit", str(timeout))
        if not isinstance(reply, Mapping) or type(reply.get("ok")) is not bool:
            raise RuntimeError("Reservation service violated its reply contract")
        if reply["ok"]:
            data = reply.get("data")
            if not isinstance(data, Mapping):
                raise RuntimeError("Reservation service violated its reply contract")
            return self._record({"result": deepcopy(data)}, request, None, None, result=data)
        error = reply.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        message = error.get("message") if isinstance(error, Mapping) else None
        if type(code) is not str or not code or len(code) > 100:
            code = "WorldError"
        if type(message) is not str:
            message = str(message)
        return self._record({"error": code, "message": message}, request, code, message)

    def _record(self, outcome: dict, request: dict, code: str | None, message: str | None, *,
                result: Mapping[str, Any] | None = None) -> Outcome:
        service = self._running()
        self._trace.append({"event_id": self._calls, "request": request, "outcome": outcome,
                            "state": _evidence_state(service), "actor_id": self._actor})
        if code is None:
            return Outcome(status="ok", event_id=self._calls, result=deepcopy(result))
        return Outcome(status="error", event_id=self._calls, error=code, message=message)

    def observe(self) -> Observation:
        service = self._running()
        return Observation(trace=deepcopy(self._trace), state=_evidence_state(service),
                           calls=self._calls)

    def evaluate(self, *, trace: list, state: Mapping[str, Any]) -> list[InvariantCheck]:
        """Invariant checks from authoritative evidence, mirroring the service mapping.

        Verdicts come from the observed state exactly like
        ``ReservationService.check_invariants``: over-capacity, negative use,
        a mismatched used total, or a malformed record. Evidence cites the
        revision-growth events for failed invariants and the measured capacity
        invariant. Pure: no adapter state is read.
        """
        if type(trace) is not list or not isinstance(state, Mapping):
            raise ValueError("Invalid reservation evidence")
        try:
            capacity, used = state["capacity"], state["used"]
            active, records = state["reservations"], state["all_reservations"]
        except KeyError:
            raise ValueError("Invalid reservation evidence") from None
        if type(capacity) is not int or type(used) is not int:
            raise ValueError("Invalid reservation evidence")
        if type(active) is not list or type(records) is not list:
            raise ValueError("Invalid reservation evidence")
        try:
            seats_list = [record["seats"] for record in active]
            pairs = [(record["seats"], record["status"]) for record in records]
        except (KeyError, TypeError):
            raise ValueError("Invalid reservation evidence") from None
        if any(type(seats) is not int for seats in seats_list):
            raise ValueError("Invalid reservation evidence")
        recomputed = sum(seats_list)
        wellformed = all(type(seats) is int and seats > 0
                         and status in ("active", "cancelled") for seats, status in pairs)
        checks = {
            "capacity_respected": used <= capacity,
            "used_nonnegative": used >= 0,
            "used_consistent": recomputed == used,
            "records_wellformed": wellformed,
        }
        growth = self._growth_or_refuse(trace)
        found = []
        for name in sorted(checks):
            measured = name == "capacity_respected"
            found.append(InvariantCheck(
                name=name, passed=checks[name],
                evidence_event_ids=tuple(growth) if (not checks[name] or measured) else (),
                observed=used if measured else None, limit=capacity if measured else None,
                unit="seats" if measured else None))
        return found

    @staticmethod
    def _growth_or_refuse(trace: list) -> list[int]:
        for event in trace:
            if not isinstance(event, Mapping):
                raise ValueError("Invalid reservation evidence")
            event_id, state = event.get("event_id"), event.get("state")
            revision = state.get("revision") if isinstance(state, Mapping) else None
            if type(event_id) is not int or type(revision) is not int:
                raise ValueError("Invalid reservation evidence")
        return revision_growth_events(trace)

    def faults_fired(self, scenario: Mapping[str, Any], trace: list) -> frozenset[str]:
        if not isinstance(scenario, Mapping) or type(trace) is not list:
            raise ValueError("Invalid fault evidence")
        for event in trace:
            if not isinstance(event, Mapping) or not isinstance(event.get("outcome"), Mapping):
                raise ValueError("Invalid fault evidence")
        fired = set()
        for event in trace:
            outcome = event["outcome"]
            if outcome.get("error") == "TimeoutAfterCommit":
                fired.add(TIMEOUT_FAULT)
            request, state = event.get("request"), event.get("state")
            if isinstance(request, Mapping) and request.get("tool") == "check_availability":
                result = outcome.get("result")
                if isinstance(result, Mapping) and result.get("stale") is True:
                    fired.add(STALE_FAULT)
            if isinstance(state, Mapping) and "used" in state and "capacity" in state:
                used, capacity = state["used"], state["capacity"]
                if type(used) is int and type(capacity) is int and used > capacity:
                    fired.add(SKIP_FAULT)
        return frozenset(fired)

    def close(self) -> None:
        if self._service is not None:
            self._service.close()
        self._closed = True

    def snapshot(self) -> Mapping[str, Any]:
        raise UnsupportedOperation(
            "reservation snapshots are not implemented; replay recorded actions instead")

    def restore(self, snapshot: Mapping[str, Any]) -> Observation:
        raise UnsupportedOperation(
            "reservation snapshots are not implemented; replay recorded actions instead")


ENVIRONMENT_MANIFEST = AdapterManifest(
    package="ghostlab.adapters.reservation_env", version=ENVIRONMENT_VERSION,
    origin="checked_in",
    description="External reservation service, vendored and reviewed (P2)")


def register_reservation_adapters(registry: AdapterRegistry | None = None) -> AdapterRegistry:
    """Register the checked-in reservation environment on a registry (fresh by default)."""
    registry = registry if registry is not None else AdapterRegistry()
    registry.register_environment(RESERVATION_WORLD_VERSION, ReservationEnvironment,
                                  ENVIRONMENT_MANIFEST)
    return registry


assert ALLOWED_TOOLS == ("check_availability", "reserve", "cancel", "get_status")
assert FAULTS == FAULT_IDS
assert RESERVATION_FIXTURES == ("empty_small", "full", "half_full", "near_full")
assert isinstance(ReservationEnvironment(), EnvironmentAdapter)
