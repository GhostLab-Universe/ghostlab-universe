"""Trusted synthetic refund world, real state transitions, independent verification.

This module is not an isolation boundary for arbitrary Python code. The world
deliberately permits cumulative over-refunds so the vulnerable adapter can be
tested. Authorization, tool schemas and call budgets are enforced separately.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any


WORLD_VERSION = "refund-world/1"
ADAPTER_VERSION = "refund-adapters/1"


class ToolError(Exception):
    pass


class PaymentTimeout(ToolError):
    pass


class BudgetExceeded(ToolError):
    pass


@dataclass(frozen=True)
class Fixture:
    order_id: str = "synthetic-order-1"
    customer_id: str = "synthetic-customer-1"
    paid_minor: int = 250_000
    currency: str = "INR"

    def __post_init__(self):
        for value in (self.order_id, self.customer_id, self.currency):
            if not isinstance(value, str) or not value or len(value) > 100:
                raise ValueError("Invalid fixture identifier")
        if type(self.paid_minor) is not int or not 0 < self.paid_minor <= 10**12:
            raise ValueError("Invalid paid_minor")


@dataclass(frozen=True)
class Scenario:
    timeout_after_commit: bool = False
    max_tool_calls: int = 20

    def __post_init__(self):
        if type(self.timeout_after_commit) is not bool:
            raise ValueError("timeout_after_commit must be boolean")
        if type(self.max_tool_calls) is not int or not 1 <= self.max_tool_calls <= 1000:
            raise ValueError("max_tool_calls must be between 1 and 1000")


class RefundWorld:
    """Authoritative ledger; one instance per baseline or faulted branch."""

    def __init__(self, fixture: Fixture, scenario: Scenario, *, actor_id: str | None = None):
        self.fixture = fixture
        self.scenario = scenario
        # The controller binds this principal; model/tool arguments cannot set it.
        # The default is for trusted local demos, not production authentication.
        self.actor_id = fixture.customer_id if actor_id is None else actor_id
        if type(self.actor_id) is not str or not self.actor_id or len(self.actor_id) > 100:
            raise ValueError("Invalid actor_id")
        self.ledger: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self.calls = 0
        self._fault_fired = False

    def snapshot(self):
        return {
            "order": asdict(self.fixture),
            "ledger": deepcopy(self.ledger),
            "total_refunded_minor": sum(row["amount_minor"] for row in self.ledger),
        }

    def call(self, tool: str, arguments: dict[str, Any]):
        if self.calls >= self.scenario.max_tool_calls:
            raise BudgetExceeded("Tool call budget exhausted")
        self.calls += 1
        request = {"tool": tool, "arguments": deepcopy(arguments)}
        try:
            result = self._dispatch(tool, arguments)
        except ToolError as error:
            self.trace.append({
                "event_id": self.calls, "request": request,
                "outcome": {"error": type(error).__name__, "message": str(error)},
                "state": self.snapshot(),
            })
            raise
        self.trace.append({
            "event_id": self.calls, "request": request,
            "outcome": {"result": deepcopy(result)}, "state": self.snapshot(),
        })
        return deepcopy(result)

    def _dispatch(self, tool, arguments):
        schemas = {
            "get_order": {"order_id", "customer_id"},
            "get_refund_status": {"order_id", "customer_id", "idempotency_key"},
            "issue_refund": {"order_id", "customer_id", "amount_minor", "idempotency_key"},
        }
        if type(tool) is not str or tool not in schemas:
            raise ToolError("Unknown tool")
        if type(arguments) is not dict or set(arguments) != schemas[tool]:
            raise ToolError("Arguments do not match tool schema")
        for key, value in arguments.items():
            if key != "amount_minor" and (
                not isinstance(value, str) or not value or len(value) > 100
            ):
                raise ToolError("Invalid string argument")
        if (self.actor_id != self.fixture.customer_id
                or arguments["customer_id"] != self.fixture.customer_id):
            raise ToolError("Customer is not authorized for this order")
        if arguments["order_id"] != self.fixture.order_id:
            raise ToolError("Order does not exist")
        if tool == "get_order":
            return asdict(self.fixture)
        existing = next((row for row in self.ledger
                         if row["idempotency_key"] == arguments["idempotency_key"]), None)
        if tool == "get_refund_status":
            return {"status": "committed" if existing else "not_found",
                    "refund": deepcopy(existing)}
        amount = arguments["amount_minor"]
        if type(amount) is not int or not 0 < amount <= self.fixture.paid_minor:
            raise ToolError("Invalid refund amount")
        if existing:
            if existing["amount_minor"] != amount:
                raise ToolError("Idempotency key reused with different amount")
            return existing
        row = {
            "transaction_id": f"refund-{len(self.ledger) + 1}",
            "order_id": self.fixture.order_id, "customer_id": arguments["customer_id"],
            "amount_minor": amount, "idempotency_key": arguments["idempotency_key"],
        }
        self.ledger.append(row)
        if self.scenario.timeout_after_commit and not self._fault_fired:
            self._fault_fired = True
            raise PaymentTimeout("Payment committed; response timed out")
        return row


def verify(fixture: Fixture, state: dict[str, Any]):
    """Compute invariants from authoritative state, never model assessments."""
    ledger = state["ledger"]
    return {
        "authorized_refunds": all(row["customer_id"] == fixture.customer_id
                                  and row["order_id"] == fixture.order_id for row in ledger),
        "total_refund_within_payment": sum(row["amount_minor"] for row in ledger)
        <= fixture.paid_minor,
        "no_duplicate_full_refund": sum(row["amount_minor"] == fixture.paid_minor
                                        for row in ledger) <= 1,
        "unique_idempotency_keys": len({row["idempotency_key"] for row in ledger})
        == len(ledger),
    }


def run_agent(world: RefundWorld, adapter: str):
    if adapter not in {"vulnerable", "repaired"}:
        raise ValueError("Unknown adapter")
    identity = {"order_id": world.fixture.order_id, "customer_id": world.fixture.customer_id}
    order = world.call("get_order", identity)
    request = {**identity, "amount_minor": order["paid_minor"],
               "idempotency_key": "refund-intent-1"}
    try:
        world.call("issue_refund", request)
    except PaymentTimeout:
        if adapter == "vulnerable":
            world.call("issue_refund", {**request, "idempotency_key": "refund-retry-2"})
        else:
            status = world.call("get_refund_status", {
                **identity, "idempotency_key": request["idempotency_key"],
            })
            if status["status"] != "committed":
                world.call("issue_refund", request)


def run(fixture: Fixture, scenario: Scenario, adapter: str, *, actor_id: str | None = None):
    world = RefundWorld(fixture, scenario, actor_id=actor_id)
    status = "completed"
    try:
        run_agent(world, adapter)
    except BudgetExceeded:
        status = "budget_exhausted"
    state = world.snapshot()
    return {"adapter": adapter, "adapter_version": ADAPTER_VERSION, "actor_id": world.actor_id,
            "status": status, "trace": world.trace, "final_state": state,
            "checks": verify(fixture, state)}


def reality_diff(baseline, faulted):
    first = None
    for index in range(max(len(baseline["trace"]), len(faulted["trace"]))):
        left = baseline["trace"][index] if index < len(baseline["trace"]) else None
        right = faulted["trace"][index] if index < len(faulted["trace"]) else None
        if left != right:
            first = {"event_id": index + 1, "baseline": left, "faulted": right}
            break
    return {
        "first_divergence": first,
        "baseline_refunded_minor": baseline["final_state"]["total_refunded_minor"],
        "faulted_refunded_minor": faulted["final_state"]["total_refunded_minor"],
        "violated_invariants": [name for name, passed in faulted["checks"].items() if not passed],
    }


def experiment(fixture: Fixture | None = None):
    fixture = fixture or Fixture()
    baseline = run(fixture, Scenario(), "vulnerable")
    faulted = run(fixture, Scenario(timeout_after_commit=True), "vulnerable")
    repaired = run(fixture, Scenario(timeout_after_commit=True), "repaired")
    return {"baseline": baseline, "faulted": faulted, "repaired": repaired,
            "diff": reality_diff(baseline, faulted)}
