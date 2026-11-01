"""Checked-in refund target adapters behind the extension interface.

``RefundTarget("vulnerable")`` retries a timed-out refund with a new
idempotency key; ``RefundTarget("repaired")`` checks refund status before any
retry. Proposals are literally what the checked-in agent calls next: each
``propose`` replays the recorded history through the module-global
``run_agent`` and captures the call it makes next, so evidence through the
generic runner is identical to the direct run by construction (tested).
``run_agent`` stays a module global so behaviour-regression tests can patch
it here, where the behaviour now lives. Deterministic: no model, no
provider, no network. Standard library only.
"""

from types import SimpleNamespace

from ghostlab.core import ADAPTER_VERSION, PaymentTimeout, ToolError, run_agent
from ghostlab.extensions.environment import Action, Observation, Outcome
from ghostlab.extensions.target import TargetAdapter, TargetInfo, TargetTask

VULNERABLE = "vulnerable"
REPAIRED = "repaired"
ADAPTER_IDS = (VULNERABLE, REPAIRED)


class _NextCall(Exception):
    """Carries the next agent tool call out of ``run_agent``.

    Deliberately not a :class:`ToolError`: the agent must not catch it.
    """

    def __init__(self, tool, arguments):
        super().__init__(tool)
        self.tool = tool
        self.arguments = arguments


class _ReplayWorld:
    """Replays recorded outcomes, then captures the next agent call.

    ``run_agent`` only uses ``world.fixture`` (order/customer ids) and
    ``world.call``. Recorded calls replay their outcomes (errors re-raised by
    name); the first call past the history is captured and proposed.
    """

    def __init__(self, fixture, history):
        self.fixture = fixture
        self._history = history
        self._calls = 0

    def call(self, tool, arguments):
        if self._calls >= len(self._history):
            raise _NextCall(tool, arguments)
        _, outcome = self._history[self._calls]
        self._calls += 1
        if outcome.status == "error":
            if outcome.error == "PaymentTimeout":
                raise PaymentTimeout(outcome.message)
            raise ToolError(outcome.message or outcome.error)
        return dict(outcome.result)


class RefundTarget:
    """One deterministic refund agent: ``vulnerable`` or ``repaired``."""

    def __init__(self, adapter_id: str):
        if adapter_id not in ADAPTER_IDS:
            raise ValueError("Unknown adapter")
        self._adapter_id = adapter_id
        self._identity: dict | None = None
        self._history: list[tuple[Action, Outcome]] = []
        self._closed_reason: str | None = None

    def info(self) -> TargetInfo:
        return TargetInfo(
            adapter_id=self._adapter_id, adapter_version=ADAPTER_VERSION, model=None,
            provider=None, endpoint=None,
            actions=tuple({"tool": action.tool, "arguments": dict(action.arguments),
                           "status": outcome.status}
                          for action, outcome in self._history))

    def start(self, task: TargetTask, observation: Observation) -> None:
        if self._identity is not None:
            raise RuntimeError("Target is already started")
        if not isinstance(task, TargetTask) or not isinstance(observation, Observation):
            raise TypeError("Target start needs a TargetTask and an Observation")
        identity = {}
        for key in ("order_id", "customer_id"):
            value = task.params.get(key)
            if type(value) is not str or not value or len(value) > 100:
                raise ValueError(f"Invalid task {key}")
            identity[key] = value
        self._identity = identity

    def _started(self) -> dict:
        if self._identity is None:
            raise RuntimeError("Target was not started")
        if self._closed_reason is not None:
            raise RuntimeError("Target is closed")
        return self._identity

    def propose(self, observation: Observation) -> Action | None:
        identity = dict(self._started())
        if not isinstance(observation, Observation):
            raise TypeError("Target proposals need an Observation")
        fixture = SimpleNamespace(order_id=identity["order_id"],
                                  customer_id=identity["customer_id"])
        replay = _ReplayWorld(fixture, self._history)
        try:
            run_agent(replay, self._adapter_id)
        except _NextCall as pending:
            return Action(tool=pending.tool, arguments=dict(pending.arguments))
        return None

    def notify_result(self, action: Action, outcome: Outcome) -> None:
        if self._identity is None:
            raise RuntimeError("Target was not started")
        if not isinstance(action, Action) or not isinstance(outcome, Outcome):
            raise TypeError("Target results need an Action and an Outcome")
        self._history.append((action, outcome))

    def close(self, reason: str) -> None:
        if type(reason) is not str or not reason:
            raise ValueError("Invalid target close reason")
        if self._closed_reason is None:
            self._closed_reason = reason


assert isinstance(RefundTarget(VULNERABLE), TargetAdapter)
assert isinstance(RefundTarget(REPAIRED), TargetAdapter)
assert not issubclass(_NextCall, ToolError)