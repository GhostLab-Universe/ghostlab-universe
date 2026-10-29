"""Provider-neutral model requests and replies (M01 boundary).

Replies are untrusted: tool names and arguments are kept raw so policy can deny
and record them. Nothing in a reply carries identity, scope or a verdict.
"""

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from .common import ErrorInfo, Ref, SchemaRef
from .evidence import ScenarioV1
from .primitives import (Count, Currency, FalseFlag, Identifier, MinorUnits, ModelId, ModelRole,
                         SchemaVersion, StrictModel, Text, UtcTimestamp)
from .tools import ToolDefinition

Provider = Literal["meta_responses", "baseten_chat_completions"]
ProviderCallId = Annotated[str, StringConstraints(min_length=1, max_length=256)]
# Opaque provider continuation data (e.g. a previous response id). Never parsed.
Continuation = Annotated[str, StringConstraints(min_length=1, max_length=65_536)]


class ToolResultForModel(StrictModel):
    provider_call_id: ProviderCallId
    output_json: Annotated[str, StringConstraints(max_length=65_536)]
    is_error: bool


class ModelRequest(StrictModel):
    schema_version: SchemaVersion
    request_id: Identifier
    campaign_id: Identifier
    task_id: Identifier
    role: ModelRole
    provider: Provider
    model: ModelId
    context_bundle: Ref
    tools: list[ToolDefinition] = Field(max_length=16)
    tool_results: list[ToolResultForModel] = Field(max_length=16)
    output_schema: SchemaRef | None
    continuation: Continuation | None
    max_output_tokens: int = Field(ge=1, le=200_000)
    timeout_ms: int = Field(ge=1, le=600_000)
    budget_reservation_id: Identifier
    stream: FalseFlag  # streaming stays off until its capability gate passes

    @model_validator(mode="after")
    def _check(self):
        if self.context_bundle.kind != "context_bundle":
            raise ValueError("context_bundle must reference a context bundle")
        return self


class ProposedToolCall(StrictModel):
    provider_call_id: ProviderCallId
    tool_name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    arguments_json: Annotated[str, StringConstraints(max_length=65_536)]


class Usage(StrictModel):
    """Reported usage. None means the provider did not report it (unknown, not zero)."""

    input_tokens: Count | None
    output_tokens: Count | None
    cost_minor: MinorUnits | None
    cost_currency: Currency | None

    @model_validator(mode="after")
    def _check(self):
        if (self.cost_minor is None) != (self.cost_currency is None):
            raise ValueError("cost_minor and cost_currency are set together")
        return self


class ModelReply(StrictModel):
    schema_version: SchemaVersion
    request_id: Identifier
    provider: Provider
    model: ModelId
    status: Literal["completed", "tool_calls", "incomplete", "refused", "failed"]
    text: Annotated[str, StringConstraints(max_length=65_536)] | None
    tool_calls: list[ProposedToolCall] = Field(max_length=16)
    structured_output: dict[str, JsonValue] | None  # validated against output_schema later
    usage: Usage
    continuation: Continuation | None
    error: ErrorInfo | None
    received_at: UtcTimestamp

    @model_validator(mode="after")
    def _check(self):
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("Only failed replies carry an error, and they must")
        if (self.status == "tool_calls") != bool(self.tool_calls):
            raise ValueError("tool_calls status requires tool calls, and only then")
        ids = [call.provider_call_id for call in self.tool_calls]
        if len(set(ids)) != len(ids):
            raise ValueError("provider_call_id values must be unique")
        return self


class ExperimentProposal(StrictModel):
    """Explorer structured output. Every field required; no identity or authority.

    Shaped for Meta strict structured output (object root, all fields required,
    additionalProperties false). Live acceptance by the model is untested.
    """

    schema_version: SchemaVersion
    hypothesis: Text
    adapter: Literal["vulnerable", "repaired"]
    scenario: ScenarioV1
    expected_signal: Text
