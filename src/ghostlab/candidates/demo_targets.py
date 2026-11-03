"""Demo naive (v1) and fixed (v2) refund targets for candidate proofs.

``DemoNaiveTarget`` refunds the full amount twice under distinct
idempotency keys -- the double-spend bug. ``DemoFixedTarget`` reads
the order, then refunds once. Both are deterministic
:class:`TargetAdapter` implementations with fixed scripts, no model,
no provider and no network. :func:`register_demo_targets` registers
them as two changed versions of the demo refund service on a caller
supplied registry, so candidate proofs run without touching the
checked-in vulnerable/repaired adapters. Standard library only.
"""

from ..extensions.environment import Action, Observation, Outcome
from ..extensions.registry import AdapterManifest, AdapterRegistry
from ..extensions.target import TargetAdapter, TargetInfo, TargetTask

WORLD_VERSION = "refund-world/1"
NAIVE_ID = "demo-refund-naive"
FIXED_ID = "demo-refund-fixed"
NAIVE_VERSION = "demo-refund-targets/1"
FIXED_VERSION = "demo-refund-targets/2"

NAIVE_MANIFEST = AdapterManifest(
    package="ghostlab.candidates.demo_targets", version=NAIVE_VERSION,
    origin="checked_in", description="Demo naive refund target (double refund)")
FIXED_MANIFEST = AdapterManifest(
    package="ghostlab.candidates.demo_targets", version=FIXED_VERSION,
    origin="checked_in", description="Demo fixed refund target (single refund)")


def _identity(task: TargetTask) -> dict:
    identity = {}
    for key in ("order_id", "customer_id"):
        value = task.params.get(key)
        if type(value) is not str or not value or len(value) > 100:
            raise ValueError(f"Invalid task {key}")
        identity[key] = value
    amount = task.params.get("amount_minor")
    if type(amount) is not int or not 0 < amount <= 10**12:
        raise ValueError("Invalid task amount_minor")
    identity["amount_minor"] = amount
    return identity


class DemoNaiveTarget:
    """Naive v1: refunds the full amount twice under distinct keys."""

    def __init__(self):
        self._identity: dict | None = None
        self._history: list[tuple[Action, Outcome]] = []
        self._closed_reason: str | None = None

    def info(self) -> TargetInfo:
        return TargetInfo(
            adapter_id=NAIVE_ID, adapter_version=NAIVE_VERSION, model=None,
            provider=None, endpoint=None,
            actions=tuple({"tool": action.tool,
                           "arguments": dict(action.arguments),
                           "status": outcome.status}
                          for action, outcome in self._history))

    def start(self, task: TargetTask, observation: Observation) -> None:
        if self._identity is not None:
            raise RuntimeError("Target is already started")
        if not isinstance(task, TargetTask) or not isinstance(observation, Observation):
            raise TypeError("Target start needs a TargetTask and an Observation")
        self._identity = _identity(task)

    def propose(self, observation: Observation) -> Action | None:
        if self._identity is None:
            raise RuntimeError("Target was not started")
        if self._closed_reason is not None:
            raise RuntimeError("Target is closed")
        if not isinstance(observation, Observation):
            raise TypeError("Target proposals need an Observation")
        step = len(self._history)
        if step >= 2:
            return None
        return Action(tool="issue_refund", arguments={
            "order_id": self._identity["order_id"],
            "customer_id": self._identity["customer_id"],
            "amount_minor": self._identity["amount_minor"],
            "idempotency_key": f"demo-naive-{step + 1}"})

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


class DemoFixedTarget:
    """Fixed v2: reads the order, then refunds exactly once."""

    def __init__(self):
        self._identity: dict | None = None
        self._history: list[tuple[Action, Outcome]] = []
        self._closed_reason: str | None = None

    def info(self) -> TargetInfo:
        return TargetInfo(
            adapter_id=FIXED_ID, adapter_version=FIXED_VERSION, model=None,
            provider=None, endpoint=None,
            actions=tuple({"tool": action.tool,
                           "arguments": dict(action.arguments),
                           "status": outcome.status}
                          for action, outcome in self._history))

    def start(self, task: TargetTask, observation: Observation) -> None:
        if self._identity is not None:
            raise RuntimeError("Target is already started")
        if not isinstance(task, TargetTask) or not isinstance(observation, Observation):
            raise TypeError("Target start needs a TargetTask and an Observation")
        self._identity = _identity(task)

    def propose(self, observation: Observation) -> Action | None:
        if self._identity is None:
            raise RuntimeError("Target was not started")
        if self._closed_reason is not None:
            raise RuntimeError("Target is closed")
        if not isinstance(observation, Observation):
            raise TypeError("Target proposals need an Observation")
        step = len(self._history)
        if step == 0:
            return Action(tool="get_order", arguments={
                "order_id": self._identity["order_id"],
                "customer_id": self._identity["customer_id"]})
        if step == 1:
            return Action(tool="issue_refund", arguments={
                "order_id": self._identity["order_id"],
                "customer_id": self._identity["customer_id"],
                "amount_minor": self._identity["amount_minor"],
                "idempotency_key": "demo-fixed-1"})
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


assert isinstance(DemoNaiveTarget(), TargetAdapter)
assert isinstance(DemoFixedTarget(), TargetAdapter)


def register_demo_targets(registry: AdapterRegistry,
                          world_version: str = WORLD_VERSION) -> AdapterRegistry:
    """Register the naive v1 and fixed v2 demo targets on ``registry``."""
    if not isinstance(registry, AdapterRegistry):
        raise TypeError("register_demo_targets needs an AdapterRegistry")
    registry.register_target(world_version, NAIVE_ID, DemoNaiveTarget,
                             NAIVE_MANIFEST)
    registry.register_target(world_version, FIXED_ID, DemoFixedTarget,
                             FIXED_MANIFEST)
    return registry


__all__ = ["FIXED_ID", "FIXED_MANIFEST", "FIXED_VERSION", "NAIVE_ID",
           "NAIVE_MANIFEST", "NAIVE_VERSION", "WORLD_VERSION",
           "DemoFixedTarget", "DemoNaiveTarget", "register_demo_targets"]
