"""Budget ceilings, reservations and usage. None means unknown, never zero."""

from typing import Literal

from pydantic import Field, model_validator

from .primitives import (Count, Currency, Identifier, MinorUnits, PositiveCount, StrictModel,
                         UtcTimestamp)

BudgetResource = Literal["model_calls", "tool_calls", "input_tokens", "output_tokens",
                         "cost_minor", "wall_clock_ms"]


class BudgetLimits(StrictModel):
    max_model_calls: Count
    max_tool_calls: Count
    max_input_tokens: Count
    max_output_tokens: Count
    max_cost_minor: MinorUnits | None
    cost_currency: Currency | None
    max_wall_clock_ms: PositiveCount
    max_parallel_runs: int = Field(ge=1, le=64)

    @model_validator(mode="after")
    def _check(self):
        if (self.max_cost_minor is None) != (self.cost_currency is None):
            raise ValueError("max_cost_minor and cost_currency are set together")
        return self


class BudgetUsage(StrictModel):
    model_calls: Count
    tool_calls: Count
    input_tokens: Count
    output_tokens: Count
    cost_minor: MinorUnits | None
    cost_currency: Currency | None
    wall_clock_ms: Count
    unknown_usage_events: Count


class BudgetReservation(StrictModel):
    schema_version: Literal[1]
    reservation_id: Identifier
    campaign_id: Identifier
    task_id: Identifier
    resource: BudgetResource
    amount: PositiveCount
    status: Literal["reserved", "reconciled", "released"]
    actual: Count | None
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def _check(self):
        if self.status == "reserved" and self.actual is not None:
            raise ValueError("A reservation has no actual usage until reconciled")
        return self
