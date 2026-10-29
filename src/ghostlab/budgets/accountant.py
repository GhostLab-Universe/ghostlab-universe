"""Budget accounting for campaigns (M05). M06 supplies ceilings; M04 makes them atomic.

What is reserved, and when:

- **Every outbound HTTP attempt** (retries included) reserves one ``model_calls``
  unit, its ``max_output_tokens`` of ``output_tokens`` and the bundle's estimated
  ``input_tokens``, through the provider's ``Runtime.attempt_gate``. The gate
  never raises: a refused reservation returns ``False`` and nothing is sent.
- **Every experiment run** reserves ``tool_calls`` for its scenario cap before the
  world is touched, reconciled to the trace length afterwards.

After a call, the final attempt is reconciled with the reply's reported usage and
earlier attempts with unknown usage (``None``), so unknown usage keeps counting
in full and is never treated as zero. Cost is never reserved: no price is
verified, so cost stays unknown.
"""

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ghostlab.contracts import BudgetLimits, BudgetReservation, BudgetUsage, ModelReply
from ghostlab.storage.budgets import BudgetExceeded, BudgetLedger

BUDGET_ACCOUNTING_VERSION = "ghostlab-budgets/1"
LIMIT_FIELDS = {"model_calls": "max_model_calls", "tool_calls": "max_tool_calls",
                "input_tokens": "max_input_tokens", "output_tokens": "max_output_tokens"}


class BudgetExhausted(Exception):
    def __init__(self, resource: str, scope: str):
        super().__init__(f"{scope} budget for {resource} is exhausted")
        self.resource = resource
        self.scope = scope


def utc_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class BudgetAccountant:
    """Campaign-level limits via the atomic ledger; per-task role ceilings on top.

    Per-task usage is seeded by the caller from durable data (the controller
    derives it from the ledger against a checkpointed baseline), so task retries
    and crash recovery continue the same count instead of starting over.
    """

    def __init__(self, ledger: BudgetLedger, campaign_id: str, limits: BudgetLimits, *,
                 clock: Callable[[], datetime], new_id: Callable[[], str] | None = None):
        self.ledger, self.campaign_id, self.limits = ledger, campaign_id, limits
        self.clock = clock
        self.new_id = new_id or (lambda: uuid.uuid4().hex)
        self._task_used: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def reserve(self, task_id: str, resource: str, amount: int, *,
                task_limit: int | None = None) -> BudgetReservation:
        """Reserve or raise BudgetExhausted. Reservation ids are unique per attempt."""
        with self._lock:
            used = self._task_used.get((task_id, resource), 0)
            if task_limit is not None and used + amount > task_limit:
                raise BudgetExhausted(resource, "task")
            reservation = BudgetReservation.model_validate({
                "schema_version": 1, "reservation_id": f"res-{self.new_id()}",
                "campaign_id": self.campaign_id, "task_id": task_id, "resource": resource,
                "amount": amount, "status": "reserved", "actual": None,
                "created_at": utc_timestamp(self.clock())})
            try:
                self.ledger.reserve(reservation,
                                    limit=getattr(self.limits, LIMIT_FIELDS[resource]))
            except BudgetExceeded as error:
                raise BudgetExhausted(resource, "campaign") from error
            self._task_used[(task_id, resource)] = used + amount
            return reservation

    def seed_task_usage(self, task_id: str, used: dict[str, int]):
        """Start a task's counters from usage already committed for it."""
        with self._lock:
            for resource, amount in used.items():
                self._task_used[(task_id, resource)] = max(0, int(amount))

    def committed(self, resource: str) -> int:
        return self.ledger.committed(self.campaign_id, resource)[0]

    def reconcile(self, reservation: BudgetReservation, actual: int | None):
        self.ledger.reconcile(reservation.reservation_id, actual)

    def release(self, reservation: BudgetReservation):
        self.ledger.release(reservation.reservation_id)

    def model_attempt_gate(self, task_id: str, *, max_output_tokens: int, input_tokens: int,
                           task_limits: BudgetLimits,
                           deadline_passed: Callable[[], bool] | None = None) -> "AttemptGate":
        return AttemptGate(self, task_id, max_output_tokens, input_tokens, task_limits,
                           deadline_passed=deadline_passed)

    def usage(self, *, wall_clock_ms: int) -> BudgetUsage:
        values, unknown = {}, 0
        for resource in LIMIT_FIELDS:
            used, missing = self.ledger.committed(self.campaign_id, resource)
            values[resource] = used
            unknown += missing
        return BudgetUsage.model_validate({
            "model_calls": values["model_calls"], "tool_calls": values["tool_calls"],
            "input_tokens": values["input_tokens"], "output_tokens": values["output_tokens"],
            "cost_minor": None, "cost_currency": None, "wall_clock_ms": wall_clock_ms,
            "unknown_usage_events": unknown})


@dataclass
class AttemptGate:
    """``Runtime.attempt_gate`` for one model call: reserve before every HTTP attempt."""

    accountant: BudgetAccountant
    task_id: str
    max_output_tokens: int
    input_tokens: int
    task_limits: BudgetLimits
    deadline_passed: Callable[[], bool] | None = None
    attempts: list[dict[str, BudgetReservation]] = field(default_factory=list)
    exhausted: BudgetExhausted | None = None

    @property
    def group_id(self) -> str:
        return f"{self.task_id}-attempts"

    def __call__(self) -> bool:
        try:
            if self.deadline_passed is not None and self.deadline_passed():
                self.exhausted = BudgetExhausted("wall_clock_ms", "campaign")
                return False  # the campaign deadline passed: send no further attempt
        except Exception as error:  # never let a clock failure escape the transport
            self.exhausted = BudgetExhausted(type(error).__name__, "deadline")
            return False
        wanted = (("model_calls", 1, self.task_limits.max_model_calls),
                  ("output_tokens", self.max_output_tokens, self.task_limits.max_output_tokens),
                  ("input_tokens", max(1, self.input_tokens), self.task_limits.max_input_tokens))
        taken: dict[str, BudgetReservation] = {}
        try:
            for resource, amount, task_limit in wanted:
                taken[resource] = self.accountant.reserve(self.task_id, resource, amount,
                                                          task_limit=task_limit)
        except BudgetExhausted as error:
            self.exhausted = error
            for reservation in taken.values():
                self.accountant.release(reservation)
            return False
        except Exception as error:  # never let a storage failure escape the transport
            self.exhausted = BudgetExhausted(type(error).__name__, "ledger")
            for reservation in taken.values():
                self.accountant.release(reservation)
            return False
        self.attempts.append(taken)
        return True

    def settle(self, reply: ModelReply):
        """Reconcile: final attempt with reported usage, earlier attempts as unknown."""
        for index, taken in enumerate(self.attempts):
            final = index == len(self.attempts) - 1
            self.accountant.reconcile(taken["model_calls"], 1)  # the attempt was sent
            self.accountant.reconcile(taken["output_tokens"],
                                      reply.usage.output_tokens if final else None)
            self.accountant.reconcile(taken["input_tokens"],
                                      reply.usage.input_tokens if final else None)
