"""References, provenance, trusted identity, errors and artifacts."""

from typing import Literal

from pydantic import Field, model_validator

from .primitives import (AgentRole, Identifier, MediaType, SchemaName, Sha256Hex, StrictModel,
                         Text, ToolName, UtcTimestamp, require_unique)

RefKind = Literal["artifact", "run", "event", "finding", "capsule", "task", "memory",
                  "approval", "context_bundle", "envelope"]


class Ref(StrictModel):
    """Typed pointer to evidence; a digest pins content when it is known."""

    kind: RefKind
    id: Identifier
    sha256: Sha256Hex | None


class SchemaRef(StrictModel):
    name: SchemaName
    version: int = Field(ge=1, le=1_000)


class Provenance(StrictModel):
    """Where content came from. Only controller/verifier/human sources are trusted."""

    source_kind: Literal["controller", "verifier", "human", "memory", "tool_result", "model_output"]
    source_ref: Ref | None
    observed_at: UtcTimestamp
    trust: Literal["trusted", "verified", "untrusted"]

    @model_validator(mode="after")
    def _check(self):
        if self.source_kind in {"tool_result", "model_output"} and self.trust != "untrusted":
            raise ValueError("Tool results and model output are always untrusted")
        return self


class Principal(StrictModel):
    """Identity derived from an authenticated session, never from a request body."""

    principal_id: Identifier
    kind: Literal["human", "service", "local_dev"]


class TrustedScope(StrictModel):
    """Authority the controller grants for one task. Model output never sets this."""

    project_id: Identifier
    principal: Principal
    role: AgentRole
    delegated_by: AgentRole | None
    allowed_tools: list[ToolName] = Field(max_length=16)
    policy_version: Identifier
    issued_by: Literal["controller"]

    @model_validator(mode="after")
    def _check(self):
        require_unique(self.allowed_tools, "allowed_tools")
        return self


ErrorCode = Literal[
    "validation_failed", "unauthenticated", "forbidden", "not_found", "conflict",
    "budget_exhausted", "cancelled", "deadline_exceeded", "rate_limited",
    "provider_transient", "provider_permanent", "provider_invalid_output",
    "tool_error", "tool_timeout", "approval_required", "approval_invalid", "internal",
]


class ErrorInfo(StrictModel):
    """Typed error. side_effect records whether a world action may have committed."""

    code: ErrorCode
    message: Text
    retryable: bool
    side_effect: Literal["none", "committed", "unknown"]
    ref: Ref | None


class ArtifactRef(StrictModel):
    schema_version: Literal[1]
    artifact_id: Identifier
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0, le=2**40)
    media_type: MediaType
    provenance: Provenance
    created_at: UtcTimestamp
