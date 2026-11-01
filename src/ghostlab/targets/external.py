"""Plain-Python external targets behind the frozen target protocol (P3).

:class:`ExternalTarget` is the plain-Python agent interface: mappings in
and out, no GhostLab imports required, so an agent developed in another
repository can implement it with the standard library alone.
:class:`ExternalTargetAdapter` routes one such agent through
``ghostlab-extensions/1`` (start/propose/result/stop): the controller-owned
runner still owns ordering, every proposal is validated as untrusted data,
and a :class:`ControlledGateway` allowlists tools and schemas before any
call can reach a world. Model/provider configuration stays on the agent
(None for deterministic scripts) and every visible action is recorded with
its outcome status. Standard library only.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ghostlab.extensions.environment import Action, Observation, Outcome
from ghostlab.extensions.registry import RegistrationError
from ghostlab.extensions.target import TargetAdapter, TargetInfo, TargetTask


def _text(value: Any, label: str, *, max_length: int = 200) -> str:
    if type(value) is not str or not value or len(value) > max_length:
        raise ValueError(f"Invalid {label}")
    return value


def _maybe_text(value: Any, label: str, *, max_length: int = 500) -> str | None:
    if value is None:
        return None
    return _text(value, label, max_length=max_length)


@runtime_checkable
class ExternalTarget(Protocol):
    """A plain-Python target agent: dicts in/out, no GhostLab imports needed.

    ``agent_id``/``agent_version`` name the tested agent; ``model``,
    ``provider`` and ``endpoint`` record its model configuration and stay
    None for deterministic scripts. ``start`` receives the task and the
    initial observation as plain mappings; ``propose`` returns the next
    ``{"tool": name, "arguments": {...}}`` mapping or None when finished;
    ``notify_result`` reports each recorded outcome in order; ``close``
    ends the run with ``finished``, ``denied``, ``budget_exhausted``,
    ``error`` or the controller stop reason. Observations are private
    copies: mutating them corrupts only the agent own view.
    """

    @property
    def agent_id(self) -> str:
        ...

    @property
    def agent_version(self) -> str:
        ...

    @property
    def model(self) -> str | None:
        ...

    @property
    def provider(self) -> str | None:
        ...

    @property
    def endpoint(self) -> str | None:
        ...

    def start(self, task: Mapping[str, Any], observation: Mapping[str, Any]) -> None:
        ...

    def propose(self, observation: Mapping[str, Any]) -> Mapping[str, Any] | None:
        ...

    def notify_result(self, action: Mapping[str, Any], outcome: Mapping[str, Any]) -> None:
        ...

    def close(self, reason: str) -> None:
        ...


def check_external_target(instance: object) -> None:
    """Refuse anything that does not satisfy the plain-Python agent interface."""
    if not isinstance(instance, ExternalTarget):
        raise RegistrationError("Not an external target: "
                                f"{type(instance).__name__} misses interface members")


@dataclass(frozen=True)
class GatewayDenial:
    """Why the controlled gateway refused one proposal. Opaque to the runner."""

    code: str
    reason: str

    def __post_init__(self):
        _text(self.code, "denial code", max_length=64)
        _text(self.reason, "denial reason", max_length=500)


class ControlledGateway:
    """Allowlist plus schema gate between untrusted proposals and the world.

    ``tools`` maps each allowed tool name to its required argument names.
    Values must be plain data: non-empty strings (at most 100 characters)
    or integers from 1 to 10**15. :meth:`authorize` returns None when a
    proposal may be dispatched, else an opaque :class:`GatewayDenial`;
    denials end the attempt without touching the world.
    """

    def __init__(self, tools: Mapping[str, Any]):
        if not isinstance(tools, Mapping) or not tools or len(tools) > 32:
            raise ValueError("Invalid gateway tools")
        cleaned: dict[str, frozenset] = {}
        for name, required in tools.items():
            _text(name, "tool name", max_length=100)
            if isinstance(required, (list, tuple, set, frozenset)):
                required = frozenset(required)
            if (not isinstance(required, frozenset) or not required or len(required) > 16
                    or any(type(arg) is not str or not arg or len(arg) > 100
                           for arg in required)):
                raise ValueError(f"Invalid required arguments for {name!r}")
            cleaned[name] = required
        self._tools = cleaned

    @property
    def tools(self) -> dict[str, frozenset]:
        """The allowlist, as a private copy."""
        return {name: frozenset(required) for name, required in self._tools.items()}

    def authorize(self, action: Action) -> GatewayDenial | None:
        """None when ``action`` may be dispatched, else the denial."""
        if not isinstance(action, Action):
            raise TypeError("Gateway authorizes Action")
        required = self._tools.get(action.tool)
        if required is None:
            return GatewayDenial("unknown_tool",
                                 f"tool {action.tool!r} is not allowlisted")
        if set(action.arguments) != set(required):
            return GatewayDenial(
                "invalid_arguments",
                f"arguments for {action.tool!r} must be exactly {sorted(required)}")
        for key, value in action.arguments.items():
            if type(value) is str:
                if not value or len(value) > 100:
                    return GatewayDenial(
                        "invalid_arguments",
                        f"argument {key!r} must be a non-empty string of at most "
                        "100 characters")
            elif type(value) is int:
                if not 1 <= value <= 10**15:
                    return GatewayDenial(
                        "invalid_arguments",
                        f"argument {key!r} must be an integer from 1 to 10**15")
            else:
                return GatewayDenial(
                    "invalid_arguments",
                    f"argument {key!r} must be a string or an integer")
        return None


def _plain_observation(observation: Observation) -> dict[str, Any]:
    """The authoritative observation as private plain data for one agent."""
    return {"trace": deepcopy(observation.trace),
            "state": deepcopy(dict(observation.state)),
            "calls": observation.calls}


class ExternalTargetAdapter:
    """Routes one plain-Python agent through the frozen target protocol.

    One instance serves one run: ``start`` begins it, ``propose`` and
    ``notify_result`` alternate until the agent finishes, and ``close``
    always ends it. Proposals are untrusted data: anything that is not
    None or a ``{"tool": str, "arguments": {...}}`` mapping raises before
    the gateway or the world sees it. :meth:`info` records the agent
    identity, its model/provider configuration and every visible action
    with its outcome status.
    """

    def __init__(self, agent: ExternalTarget):
        check_external_target(agent)
        _text(agent.agent_id, "agent id")
        _text(agent.agent_version, "agent version")
        for field in ("model", "provider", "endpoint"):
            _maybe_text(getattr(agent, field), field)
        self._agent = agent
        self._history: list[tuple[Action, Outcome]] = []
        self._close_reason: str | None = None
        self._started = False

    @property
    def agent(self) -> ExternalTarget:
        """The wrapped plain-Python agent."""
        return self._agent

    @property
    def close_reason(self) -> str | None:
        """The reason this run ended with, once closed."""
        return self._close_reason

    def info(self) -> TargetInfo:
        agent = self._agent
        return TargetInfo(
            adapter_id=agent.agent_id, adapter_version=agent.agent_version,
            model=agent.model, provider=agent.provider, endpoint=agent.endpoint,
            actions=tuple({"tool": action.tool, "arguments": dict(action.arguments),
                           "status": outcome.status}
                          for action, outcome in self._history))

    def start(self, task: TargetTask, observation: Observation) -> None:
        if self._started:
            raise RuntimeError("Target is already started")
        if not isinstance(task, TargetTask) or not isinstance(observation, Observation):
            raise TypeError("Target start needs a TargetTask and an Observation")
        self._agent.start({"task_id": task.task_id,
                           "params": deepcopy(dict(task.params))},
                          _plain_observation(observation))
        self._started = True

    def propose(self, observation: Observation) -> Action | None:
        if not self._started:
            raise RuntimeError("Target was not started")
        if self._close_reason is not None:
            raise RuntimeError("Target is closed")
        if not isinstance(observation, Observation):
            raise TypeError("Target proposals need an Observation")
        proposal = self._agent.propose(_plain_observation(observation))
        if proposal is None:
            return None
        if (not isinstance(proposal, Mapping) or type(proposal.get("tool")) is not str
                or not isinstance(proposal.get("arguments"), Mapping)):
            raise TypeError("External target proposals must be None or "
                            "{tool, arguments} mappings")
        arguments = proposal["arguments"]
        if any(type(key) is not str for key in arguments):
            raise TypeError("External target argument names must be strings")
        try:
            return Action(tool=proposal["tool"], arguments=dict(arguments))
        except ValueError as error:
            raise TypeError(f"Invalid external target proposal: {error}") from None

    def notify_result(self, action: Action, outcome: Outcome) -> None:
        if not self._started:
            raise RuntimeError("Target was not started")
        if not isinstance(action, Action) or not isinstance(outcome, Outcome):
            raise TypeError("Target results need an Action and an Outcome")
        self._history.append((action, outcome))
        self._agent.notify_result(
            {"tool": action.tool, "arguments": deepcopy(dict(action.arguments))},
            {"status": outcome.status, "event_id": outcome.event_id,
             "result": (deepcopy(dict(outcome.result))
                        if outcome.result is not None else None),
             "error": outcome.error, "message": outcome.message})

    def close(self, reason: str) -> None:
        if type(reason) is not str or not reason:
            raise ValueError("Invalid target close reason")
        if self._close_reason is None:
            self._close_reason = reason
            self._agent.close(reason)


class _ProtocolProbe:
    """Minimal plain-Python agent proving the adapter satisfies TargetAdapter."""

    agent_id = "probe"
    agent_version = "probe/0"
    model = None
    provider = None
    endpoint = None

    def start(self, task, observation):
        pass

    def propose(self, observation):
        return None

    def notify_result(self, action, outcome):
        pass

    def close(self, reason):
        pass


assert isinstance(_ProtocolProbe(), ExternalTarget)
assert isinstance(ExternalTargetAdapter(_ProtocolProbe()), TargetAdapter)
