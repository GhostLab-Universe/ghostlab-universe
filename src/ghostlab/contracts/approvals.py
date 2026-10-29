"""Bound, single-use human approvals (M06 decides, M04 persists, M05 resumes)."""

from typing import Literal

from pydantic import Field, model_validator

from .common import Principal, Ref, TrustedScope
from .primitives import (AgentRole, Identifier, Sha256Hex, ShortText, StrictModel, Text,
                         UtcTimestamp, parse_timestamp)

ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "consumed"]


class ApprovalRequest(StrictModel):
    schema_version: Literal[1]
    approval_id: Identifier
    campaign_id: Identifier
    task_id: Identifier
    action_kind: Literal["apply_repair", "change_policy", "execute_consequential_action"]
    action_digest: Sha256Hex  # digest of the exact action payload
    payload: Ref
    summary: Text
    material_risks: list[ShortText] = Field(min_length=1, max_length=16)
    requested_by_role: AgentRole
    scope: TrustedScope
    policy_version: Identifier
    status: ApprovalStatus
    created_at: UtcTimestamp
    expires_at: UtcTimestamp

    @model_validator(mode="after")
    def _check(self):
        if parse_timestamp(self.expires_at) <= parse_timestamp(self.created_at):
            raise ValueError("expires_at must follow created_at")
        if self.payload.sha256 is not None and self.payload.sha256 != self.action_digest:
            raise ValueError("payload digest does not match action_digest")
        return self


class ApprovalSubmission(StrictModel):
    """Client request body. The approver is taken from the session, never from here."""

    schema_version: Literal[1]
    approval_id: Identifier
    action_digest: Sha256Hex
    decision: Literal["approve", "reject"]
    reason: Text | None


class ApprovalDecision(StrictModel):
    schema_version: Literal[1]
    decision_id: Identifier
    approval_id: Identifier
    action_digest: Sha256Hex
    decision: Literal["approve", "reject"]
    approver: Principal
    policy_version: Identifier
    reason: Text | None
    decided_at: UtcTimestamp
