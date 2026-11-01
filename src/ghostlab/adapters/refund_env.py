"""Checked-in refund environment adapter behind the extension interface.

:class:`RefundEnvironment` wraps the trusted synthetic refund world
(:mod:`ghostlab.core`): one INR 2,500 order whose first refund may commit and
then time out. Every applied action delegates to ``RefundWorld.call``, so the
trace, state and checks through the generic runner are identical to the
direct core run (tested). Standard library only.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from ghostlab.core import (WORLD_VERSION, BudgetExceeded, Fixture, RefundWorld, Scenario,
                           ToolError)
from ghostlab.extensions.environment import (Action, ActionBudgetExceeded,
                                             EnvironmentAdapter, EnvironmentDescription,
                                             FaultSpec, InvariantCheck, Observation, Outcome,
                                             ToolSpec, UnsupportedOperation)

REFUND_WORLD_VERSION = WORLD_VERSION
ENVIRONMENT_ID = "refund-environment"
ENVIRONMENT_VERSION = "refund-environment/1"
SOURCE = "ghostlab.adapters.refund_env (checked-in)"
FIXTURE_NAME = "synthetic-inr-2500"
TIMEOUT_FAULT = "timeout_after_commit"

REFUND_INVARIANTS = ("authorized_refunds", "no_duplicate_full_refund",
                     "total_refund_within_payment", "unique_idempotency_keys")

_ID = {"type": "string", "minLength": 1, "maxLength": 100}
_AMOUNT = {"type": "integer", "minimum": 1, "maximum": 10**15}


def _object(**fields: dict) -> dict:
    """Flat tool parameter schema, mirroring the reviewed world pack."""
    return {"type": "object", "additionalProperties": False, "required": sorted(fields),
            "properties": {name: dict(spec) for name, spec in sorted(fields.items())}}


REFUND_TOOL_SCHEMAS = {
    "get_order": _object(order_id=_ID, customer_id=_ID),
    "issue_refund": _object(order_id=_ID, customer_id=_ID, amount_minor=_AMOUNT,
                            idempotency_key=_ID),
    "get_refund_status": _object(order_id=_ID, customer_id=_ID, idempotency_key=_ID),
}


def ledger_change_events(trace: list) -> list[int]:
    """Event ids at which the authoritative ledger grew.

    The single evidence rule shared by the engine's findings and
    :meth:`RefundEnvironment.evaluate`, so both paths cite the same events.
    """
    events, size = [], 0
    for event in trace:
        grown = len(event["state"]["ledger"])
        if grown > size:
            events.append(event["event_id"])
            size = grown
    return events


def _describe() -> EnvironmentDescription:
    return EnvironmentDescription(
        world_version=REFUND_WORLD_VERSION, environment_id=ENVIRONMENT_ID,
        environment_version=ENVIRONMENT_VERSION,
        tools=[ToolSpec(name="get_order", effect="read",
                        parameters=REFUND_TOOL_SCHEMAS["get_order"],
                        description="Read the synthetic order for the bound customer"),
               ToolSpec(name="issue_refund", effect="mutate",
                        parameters=REFUND_TOOL_SCHEMAS["issue_refund"],
                        description="Issue a refund with an idempotency key"),
               ToolSpec(name="get_refund_status", effect="read",
                        parameters=REFUND_TOOL_SCHEMAS["get_refund_status"],
                        description="Look up a refund by idempotency key")],
        faults=[FaultSpec(fault_id=TIMEOUT_FAULT,
                          trigger_points=("first_issue_refund_commit",),
                          description="The first refund commits, then its response times out")],
        invariants=list(REFUND_INVARIANTS), fixtures=[FIXTURE_NAME],
        capabilities={"snapshot_restore": True}, source=SOURCE)


class RefundEnvironment:
    """The synthetic refund world as an extension environment adapter."""

    def __init__(self):
        self._world: RefundWorld | None = None
        self._closed = False
        self._description = _describe()

    def describe(self) -> EnvironmentDescription:
        return self._description

    def named_fixture(self, name: str) -> Mapping[str, Any]:
        if name != FIXTURE_NAME:
            raise KeyError(f"Unknown {REFUND_WORLD_VERSION} fixture {name!r}")
        return asdict(Fixture())

    def reset(self, fixture: Mapping[str, Any], scenario: Mapping[str, Any], *,
              actor_id: str) -> Observation:
        if not isinstance(fixture, Mapping) or not isinstance(scenario, Mapping):
            raise ValueError("Invalid fixture or scenario")
        try:
            parsed_fixture = Fixture(**dict(fixture))
            parsed_scenario = Scenario(**dict(scenario))
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid fixture or scenario: {error}") from None
        self._world = RefundWorld(parsed_fixture, parsed_scenario, actor_id=actor_id)
        self._closed = False
        return self.observe()

    def _running(self) -> RefundWorld:
        if self._world is None or self._closed:
            raise RuntimeError("Environment is not running (reset first)")
        return self._world

    def execute(self, action: Action) -> Outcome:
        world = self._running()
        if not isinstance(action, Action):
            raise TypeError("Environment actions must be Action")
        try:
            result = world.call(action.tool, dict(action.arguments))
        except BudgetExceeded:
            raise ActionBudgetExceeded("Tool call budget exhausted") from None
        except ToolError as error:
            return Outcome(status="error", event_id=world.calls, error=type(error).__name__,
                           message=str(error))
        return Outcome(status="ok", event_id=world.calls, result=result)

    def observe(self) -> Observation:
        world = self._running()
        return Observation(trace=deepcopy(world.trace), state=world.snapshot(),
                           calls=world.calls)

    def evaluate(self, *, trace: list, state: Mapping[str, Any]) -> list[InvariantCheck]:
        """Invariant checks from authoritative evidence, mirroring the engine mapping.

        Verdicts come from the ledger in ``state``; evidence cites the ledger
        growth events for failed invariants and the total invariant, and
        measured values appear only on the total invariant.
        """
        if type(trace) is not list or not isinstance(state, Mapping):
            raise ValueError("Invalid refund evidence")
        try:
            order, ledger = state["order"], state["ledger"]
            checks = {
                "authorized_refunds": all(row["customer_id"] == order["customer_id"]
                                          and row["order_id"] == order["order_id"]
                                          for row in ledger),
                "total_refund_within_payment": sum(row["amount_minor"] for row in ledger)
                <= order["paid_minor"],
                "no_duplicate_full_refund": sum(row["amount_minor"] == order["paid_minor"]
                                                for row in ledger) <= 1,
                "unique_idempotency_keys":
                len({row["idempotency_key"] for row in ledger}) == len(ledger),
            }
            total = sum(row["amount_minor"] for row in ledger)
            paid = order["paid_minor"]
        except (KeyError, TypeError):
            raise ValueError("Invalid refund evidence") from None
        if type(paid) is not int:
            raise ValueError("Invalid refund evidence")
        growth = ledger_change_events(trace)
        found = []
        for name in sorted(checks):
            totals = name == "total_refund_within_payment"
            found.append(InvariantCheck(
                name=name, passed=checks[name],
                evidence_event_ids=tuple(growth) if (not checks[name] or totals) else (),
                observed=total if totals else None, limit=paid if totals else None,
                unit="minor_units" if totals else None))
        return found

    def faults_fired(self, scenario: Mapping[str, Any], trace: list) -> frozenset[str]:
        if not isinstance(scenario, Mapping) or type(trace) is not list:
            raise ValueError("Invalid fault evidence")
        for event in trace:
            if not isinstance(event, Mapping) or not isinstance(
                    event.get("outcome"), Mapping):
                raise ValueError("Invalid fault evidence")
        timed_out = any(event["outcome"].get("error") == "PaymentTimeout" for event in trace)
        return frozenset({TIMEOUT_FAULT}) if timed_out else frozenset()

    def close(self) -> None:
        self._closed = True

    def snapshot(self) -> Mapping[str, Any]:
        world = self._running()
        return {"ledger": deepcopy(world.ledger), "trace": deepcopy(world.trace),
                "calls": world.calls, "fault_fired": world._fault_fired}

    def restore(self, snapshot: Mapping[str, Any]) -> Observation:
        if self._world is None:
            raise RuntimeError("Environment was never reset")
        if not isinstance(snapshot, Mapping):
            raise ValueError("Invalid refund snapshot")
        try:
            ledger, trace, calls, fired = (snapshot["ledger"], snapshot["trace"],
                                           snapshot["calls"], snapshot["fault_fired"])
        except KeyError:
            raise ValueError("Invalid refund snapshot") from None
        if (type(ledger) is not list or type(trace) is not list or type(calls) is not int
                or type(fired) is not bool or calls != len(trace)):
            raise ValueError("Invalid refund snapshot")
        world = self._world
        world.ledger, world.trace, world.calls = (deepcopy(ledger), deepcopy(trace), calls)
        world._fault_fired = fired
        self._closed = False
        return self.observe()


assert isinstance(RefundEnvironment(), EnvironmentAdapter)