"""Versioned skills and role-scoped context bundles (consumed by M03/M01)."""

from typing import Literal

from pydantic import Field, model_validator

from .common import Provenance, Ref, SchemaRef, TrustedScope
from .primitives import (Identifier, LongText, ModelRole, ShortText, StrictModel, Text,
                         ToolName, UtcTimestamp, require_unique)


class SkillRef(StrictModel):
    skill_id: Identifier
    version: int = Field(ge=1, le=10_000)


class SkillSpec(StrictModel):
    """A reviewed procedure. allowed_tools narrows a scope; it never grants one."""

    schema_version: Literal[1]
    skill_id: Identifier
    version: int = Field(ge=1, le=10_000)
    prompt_version: Identifier
    purpose: Text
    preconditions: list[ShortText] = Field(max_length=16)
    allowed_tools: list[ToolName] = Field(max_length=16)
    steps: list[Text] = Field(min_length=1, max_length=32)
    output_schema: SchemaRef
    stop_conditions: list[ShortText] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _check(self):
        require_unique(self.allowed_tools, "allowed_tools")
        return self


class ContextItem(StrictModel):
    item_id: Identifier
    kind: Literal["instruction", "finding", "observation", "summary", "schema", "question"]
    content: LongText
    artifact: Ref | None  # large outputs travel by reference
    provenance: Provenance

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "instruction" and (self.provenance.source_kind != "controller"
                                           or self.provenance.trust != "trusted"):
            raise ValueError("Instructions may only come from the trusted controller")
        return self


class ContextBundle(StrictModel):
    schema_version: Literal[1]
    bundle_id: Identifier
    campaign_id: Identifier
    task_id: Identifier
    role: ModelRole
    scope: TrustedScope
    objective: Text
    success_criteria: list[ShortText] = Field(min_length=1, max_length=16)
    prerequisite_refs: list[Ref] = Field(max_length=64)
    items: list[ContextItem] = Field(max_length=256)
    skill: SkillRef | None
    prompt_version: Identifier
    output_schema: SchemaRef
    token_budget: int = Field(ge=1, le=2_000_000)
    estimated_tokens: int = Field(ge=0, le=2_000_000)
    compiled_at: UtcTimestamp

    @model_validator(mode="after")
    def _check(self):
        if self.scope.role != self.role:
            raise ValueError("scope must be issued for the bundle role")
        if self.estimated_tokens > self.token_budget:
            raise ValueError("Context exceeds its token budget")
        require_unique([item.item_id for item in self.items], "item ids")
        return self
