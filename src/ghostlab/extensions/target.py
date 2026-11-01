"""Target-agent adapter interface (``ghostlab-extensions/1``): the tested agent side.

A tested agent -- a deterministic script or a model-driven policy -- implements
:class:`TargetAdapter`. The runner starts it with a task and the initial
observation, asks it to propose one tool call at a time, reports each outcome,
and closes it when the run finishes, is denied, exhausts its budget or stops
early. Proposals are untrusted data: the controller validates every one before
the environment sees it. Standard library only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .environment import Action, Observation, Outcome

FINISHED = "finished"
DENIED = "denied"
BUDGET_EXHAUSTED = "budget_exhausted"
ERROR = "error"


@dataclass(frozen=True)
class TargetTask:
    """What the target must do: an id plus world-defined task parameters."""

    task_id: str
    params: Mapping[str, Any]

    def __post_init__(self):
        if type(self.task_id) is not str or not self.task_id or len(self.task_id) > 200:
            raise ValueError("Invalid task id")
        if not isinstance(self.params, Mapping):
            raise ValueError("Invalid task params")


@dataclass(frozen=True)
class TargetInfo:
    """Who the target is and what it visibly did.

    ``model``/``provider``/``endpoint`` record the model configuration for
    model-driven targets and stay None for deterministic scripts. ``actions``
    lists every proposal the target made with its outcome status, in order.
    """

    adapter_id: str
    adapter_version: str
    model: str | None
    provider: str | None
    endpoint: str | None
    actions: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        for value, label in ((self.adapter_id, "adapter id"),
                             (self.adapter_version, "adapter version")):
            if type(value) is not str or not value or len(value) > 200:
                raise ValueError(f"Invalid {label}")
        for value, label in ((self.model, "model"), (self.provider, "provider"),
                             (self.endpoint, "endpoint")):
            if value is not None and (type(value) is not str or not value
                                      or len(value) > 500):
                raise ValueError(f"Invalid {label}")
        if not isinstance(self.actions, (list, tuple)) or len(self.actions) > 1000:
            raise ValueError("Invalid target actions")
        for action in self.actions:
            if (not isinstance(action, Mapping) or type(action.get("tool")) is not str
                    or not isinstance(action.get("arguments"), Mapping)
                    or action.get("status") not in ("ok", "error")):
                raise ValueError("Invalid target action")
        object.__setattr__(self, "actions", tuple(self.actions))


@runtime_checkable
class TargetAdapter(Protocol):
    """The tested agent behind one adapter id.

    One instance serves one run: ``start`` begins it, ``propose`` and
    ``notify_result`` alternate until the target finishes (``propose`` returns
    None) or the runner ends the attempt, and ``close`` always ends it. The
    runner stops proposing on cancellation and limits; the target must then
    simply stop, releasing its resources in ``close``.
    """

    def info(self) -> TargetInfo:
        """Adapter identity, model/provider configuration and visible actions."""
        ...

    def start(self, task: TargetTask, observation: Observation) -> None:
        """Begin one run with its task and the initial observation."""
        ...

    def propose(self, observation: Observation) -> Action | None:
        """Propose the next tool call, or None when the task is finished."""
        ...

    def notify_result(self, action: Action, outcome: Outcome) -> None:
        """Report the recorded outcome of one proposed action, in order."""
        ...

    def close(self, reason: str) -> None:
        """End the run: ``finished``, ``denied``, ``budget_exhausted``,
        ``error``, or the controller's stop reason (cancellation, deadline)."""
        ...