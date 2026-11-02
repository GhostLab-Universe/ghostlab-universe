"""Controlled gateway: all target-agent tool calls route through here.

Stdlib-only. The gateway enforces per-run budgets, per-agent allowlists and
cancellation, and records every routed action for observation.

Mediation boundary (honest): only calls made via gateway.dispatch() are
mediated. A world object held directly by caller code can still be invoked
via world.call(). Track B does not sandbox arbitrary code; local execution
is not a security boundary for unknown agents.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field
from typing import Any, Protocol


class GatewayError(Exception):
    pass


class GatewayNotAllowed(GatewayError):
    pass


class GatewayBudgetExceeded(GatewayError):
    pass


class GatewayCancelled(GatewayError):
    pass


class GatewayValidationError(GatewayError):
    pass


class WorldProtocol(Protocol):
    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    def snapshot(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ActionRecord:
    seq: int
    run_id: str
    agent_name: str
    agent_version: str
    tool: str
    arguments: dict[str, Any]
    outcome: dict[str, Any]  # {"result": ...} or {"error": ..., "message": ...}
    cancelled: bool = False


@dataclass
class _RunState:
    run_id: str
    agent_name: str
    agent_version: str
    allowed_tools: tuple[str, ...]
    max_tool_calls: int
    calls: int = 0
    cancelled: bool = False
    records: list[ActionRecord] = field(default_factory=list)


class ControlledGateway:
    """Routes agent tool calls to a bound world with limits and records."""

    def __init__(self, registry, *, default_max_tool_calls: int = 20):
        from .registry import TargetRegistry

        if not isinstance(registry, TargetRegistry):
            raise ValueError("ControlledGateway requires a TargetRegistry")
        if type(default_max_tool_calls) is not int or not 1 <= default_max_tool_calls <= 1000:
            raise ValueError("default_max_tool_calls must be between 1 and 1000")
        self._registry = registry
        self._default_max = default_max_tool_calls
        self._runs: dict[str, _RunState] = {}
        self._counter = itertools.count(1)
        self._action_seq = itertools.count(1)

    def create_run(self, agent_name: str, *, max_tool_calls: int | None = None) -> str:
        record = self._registry.get(agent_name)
        if record is None:
            raise GatewayValidationError(f"Unknown agent: {agent_name}")
        limit = self._default_max if max_tool_calls is None else max_tool_calls
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("max_tool_calls must be between 1 and 1000")
        run_id = f"run-{next(self._counter)}"
        self._runs[run_id] = _RunState(
            run_id=run_id,
            agent_name=record.name,
            agent_version=record.version,
            allowed_tools=tuple(record.allowed_tools),
            max_tool_calls=limit,
        )
        return run_id

    def cancel(self, run_id: str) -> None:
        state = self._runs.get(run_id)
        if state is None:
            raise GatewayValidationError(f"Unknown run: {run_id}")
        state.cancelled = True

    @property
    def action_records(self) -> list[ActionRecord]:
        ordered: list[ActionRecord] = []
        for state in self._runs.values():
            ordered.extend(state.records)
        return copy.deepcopy(sorted(ordered, key=lambda record: record.seq))

    def records_for_run(self, run_id: str) -> list[ActionRecord]:
        state = self._runs.get(run_id)
        if state is None:
            raise GatewayValidationError(f"Unknown run: {run_id}")
        return copy.deepcopy(list(state.records))

    def dispatch(
        self, run_id: str, world: WorldProtocol, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._runs.get(run_id)
        if state is None:
            raise GatewayValidationError(f"Unknown run: {run_id}")
        if state.cancelled:
            raise GatewayCancelled(f"Run cancelled: {run_id}")
        if type(tool) is not str or not tool:
            raise GatewayValidationError("Tool name must be a non-empty string")
        if tool not in state.allowed_tools:
            raise GatewayNotAllowed(f"Tool not allowlisted for {state.agent_name}: {tool}")
        if type(arguments) is not dict:
            raise GatewayValidationError("Arguments must be an object")
        if state.calls >= state.max_tool_calls:
            raise GatewayBudgetExceeded(f"Tool call budget exhausted for {run_id}")
        state.calls += 1
        args_copy = copy.deepcopy(arguments)
        try:
            result = world.call(tool, args_copy)
        except GatewayError:
            raise
        except Exception as error:  # World ToolErrors become observable error records.
            outcome = {"error": type(error).__name__, "message": str(error)}
            state.records.append(
                ActionRecord(
                    seq=next(self._action_seq),
                    run_id=run_id,
                    agent_name=state.agent_name,
                    agent_version=state.agent_version,
                    tool=tool,
                    arguments=copy.deepcopy(args_copy),
                    outcome=copy.deepcopy(outcome),
                )
            )
            raise
        outcome = {"result": copy.deepcopy(result)}
        state.records.append(
            ActionRecord(
                seq=next(self._action_seq),
                run_id=run_id,
                agent_name=state.agent_name,
                agent_version=state.agent_version,
                tool=tool,
                arguments=copy.deepcopy(args_copy),
                outcome=copy.deepcopy(outcome),
            )
        )
        return copy.deepcopy(result)
