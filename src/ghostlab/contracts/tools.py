"""Typed refund-world tools, normalized invocations and results.

Argument limits mirror the trusted core (non-empty strings up to 100 chars).
The world, not this schema, decides authorization and refund ceilings.
"""

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from .common import ErrorInfo
from .primitives import (Identifier, PositiveMinorUnits, SchemaVersion, StrictModel, Text,
                         ToolName, WorldVersion)

ToolArg = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class OrderRecord(StrictModel):
    order_id: ToolArg
    customer_id: ToolArg
    paid_minor: int = Field(ge=1, le=10**12)
    currency: ToolArg  # v1 core accepts any short string; synthetic fixtures use "INR"


class LedgerRow(StrictModel):
    transaction_id: ToolArg
    order_id: ToolArg
    customer_id: ToolArg
    amount_minor: PositiveMinorUnits
    idempotency_key: ToolArg


class RefundStatus(StrictModel):
    status: Literal["committed", "not_found"]
    refund: LedgerRow | None


class GetOrderArgs(StrictModel):
    order_id: ToolArg
    customer_id: ToolArg


class GetRefundStatusArgs(StrictModel):
    order_id: ToolArg
    customer_id: ToolArg
    idempotency_key: ToolArg


class IssueRefundArgs(StrictModel):
    order_id: ToolArg
    customer_id: ToolArg
    amount_minor: PositiveMinorUnits
    idempotency_key: ToolArg


class GetOrderCall(StrictModel):
    tool: Literal["get_order"]
    arguments: GetOrderArgs


class GetRefundStatusCall(StrictModel):
    tool: Literal["get_refund_status"]
    arguments: GetRefundStatusArgs


class IssueRefundCall(StrictModel):
    tool: Literal["issue_refund"]
    arguments: IssueRefundArgs


ToolCall = Annotated[GetOrderCall | GetRefundStatusCall | IssueRefundCall,
                     Field(discriminator="tool")]
ToolOutput = OrderRecord | RefundStatus | LedgerRow


class ToolDefinition(StrictModel):
    name: ToolName
    world_version: WorldVersion
    description: Text
    parameters: dict[str, JsonValue]


class ToolInvocation(StrictModel):
    """A validated, allowlisted call the controller may dispatch."""

    schema_version: SchemaVersion
    invocation_id: Identifier
    run_id: Identifier
    provider_call_id: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None
    call: ToolCall


class ToolResult(StrictModel):
    schema_version: SchemaVersion
    invocation_id: Identifier
    provider_call_id: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None
    event_id: int | None = Field(ge=1, le=1_000)
    status: Literal["ok", "error"]
    output: ToolOutput | None
    error: ErrorInfo | None

    @model_validator(mode="after")
    def _check(self):
        expected = (True, False) if self.status == "ok" else (False, True)
        if (self.output is not None, self.error is not None) != expected:
            raise ValueError("ok results carry output only; error results carry error only")
        return self
