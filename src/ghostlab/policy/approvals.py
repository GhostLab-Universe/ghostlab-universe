"""Approval domain: bound, expiring, single-use human decisions.

A request binds the exact action (sha256 of its canonical JSON), scope, policy
version and expiry. Only a session principal of kind ``human`` (or
``local_dev`` while local-dev mode is active) that an injected
``ApproverAuthority`` authorizes for the approval's project can decide; model
roles are not principals and never approve. Knowing an approval id or action
digest is not authority. Consumption is single use through
``ApprovalStore.consume``. A changed action has a different digest and needs a
new request. M04 persists, M05 pauses/resumes, M07 exposes, M08 displays.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from ghostlab.contracts import (ApprovalDecision, ApprovalRequest, ApprovalSubmission, Principal,
                                TrustedScope)
from ghostlab.storage.interfaces import ApprovalStore, Conflict, NotFound

from .authorization import POLICY_VERSION, authorize_capability

MAX_APPROVAL_TTL = timedelta(hours=24)

ApprovalDenial = Literal["capability_denied", "not_found", "not_pending", "not_approved",
                         "expired", "digest_mismatch", "policy_changed", "approver_not_allowed",
                         "approver_not_authorized", "invalid_ttl", "conflict"]


class ApprovalDenied(Exception):
    def __init__(self, code: ApprovalDenial, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code


@runtime_checkable
class ApproverAuthority(Protocol):
    """Who may decide approvals in a project. Supplied by the trusted host (M07)."""

    def may_approve(self, principal: Principal, project_id: str) -> bool: ...


class ProjectApproverAllowlist:
    """Explicit local-MVP authority: project id -> principal ids allowed to approve.

    Deny by default: an unlisted project or principal has no approval authority.
    """

    def __init__(self, approvers: Mapping[str, Iterable[str]]):
        self._approvers = MappingProxyType(
            {project: frozenset(ids) for project, ids in approvers.items()})

    def may_approve(self, principal: Principal, project_id: str) -> bool:
        return principal.principal_id in self._approvers.get(project_id, frozenset())


def action_digest(action: Mapping) -> str:
    """sha256 over canonical JSON; any change to the action changes the digest."""
    text = json.dumps(action, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _ts(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ApprovalService:
    def __init__(self, store: ApprovalStore, *, approvers: ApproverAuthority,
                 local_dev_mode: bool = False, policy_version: str = POLICY_VERSION):
        if not isinstance(approvers, ApproverAuthority):
            raise TypeError("approvers must implement ApproverAuthority")
        self._store = store
        self._approvers = approvers
        self._local_dev_mode = local_dev_mode
        self._policy_version = policy_version

    def request(self, *, scope: TrustedScope, approval_id: str, campaign_id: str, task_id: str,
                action_kind: str, action: Mapping, payload_ref_id: str, summary: str,
                material_risks: list[str], now: datetime, ttl: timedelta) -> ApprovalRequest:
        decision = authorize_capability(scope, "request_approval", project_id=scope.project_id)
        if not decision.allowed:
            raise ApprovalDenied("capability_denied", decision.reason)
        if not timedelta(0) < ttl <= MAX_APPROVAL_TTL:
            raise ApprovalDenied("invalid_ttl", f"ttl must be within {MAX_APPROVAL_TTL}")
        digest = action_digest(action)
        request = ApprovalRequest.model_validate({
            "schema_version": 1, "approval_id": approval_id, "campaign_id": campaign_id,
            "task_id": task_id, "action_kind": action_kind, "action_digest": digest,
            "payload": {"kind": "artifact", "id": payload_ref_id, "sha256": digest},
            "summary": summary, "material_risks": material_risks,
            "requested_by_role": scope.role, "scope": scope.model_dump(),
            "policy_version": self._policy_version, "status": "pending",
            "created_at": _ts(now), "expires_at": _ts(now + ttl),
        })
        try:
            self._store.create(request)
        except Conflict as error:
            raise ApprovalDenied("conflict", "approval_id already exists") from error
        return request

    def _get(self, project_id: str, approval_id: str) -> ApprovalRequest:
        try:
            return self._store.get(project_id, approval_id)
        except NotFound as error:
            raise ApprovalDenied("not_found", "no such approval in this project") from error

    def get_request(self, *, project_id: str, approval_id: str) -> ApprovalRequest:
        """Read one approval request (status polling for lifecycle cleanup)."""
        return self._get(project_id, approval_id)

    def decide(self, *, session_principal: Principal, project_id: str,
               submission: ApprovalSubmission, decision_id: str,
               now: datetime) -> ApprovalDecision:
        """Record a human decision.

        ``session_principal`` and ``project_id`` come from M07's authenticated
        session, never from the request body. Authority is checked before the
        approval is looked up, so an unauthorized caller learns nothing about
        which approvals exist.
        """
        if session_principal.kind == "service" or (
                session_principal.kind == "local_dev" and not self._local_dev_mode):
            raise ApprovalDenied("approver_not_allowed",
                                 f"{session_principal.kind} principals cannot approve here")
        if not self._approvers.may_approve(session_principal, project_id):
            raise ApprovalDenied("approver_not_authorized",
                                 "principal has no approval authority in this project")
        request = self._get(project_id, submission.approval_id)
        if request.status != "pending":
            raise ApprovalDenied("not_pending", f"approval is {request.status}")
        if now >= _parse(request.expires_at):
            try:
                self._store.mark_expired(project_id, request.approval_id)
            except Conflict:
                pass  # another caller already moved it; it is not pending either way
            raise ApprovalDenied("expired", "approval request has expired")
        if submission.action_digest != request.action_digest:
            raise ApprovalDenied("digest_mismatch", "decision is for a different action")
        if request.policy_version != self._policy_version:
            raise ApprovalDenied("policy_changed", "policy changed since the request")
        decision = ApprovalDecision.model_validate({
            "schema_version": 1, "decision_id": decision_id,
            "approval_id": request.approval_id, "action_digest": request.action_digest,
            "decision": submission.decision, "approver": session_principal.model_dump(),
            "policy_version": self._policy_version, "reason": submission.reason,
            "decided_at": _ts(now),
        })
        new_status = "approved" if submission.decision == "approve" else "rejected"
        try:
            self._store.record_decision(project_id, decision, new_status=new_status)
        except Conflict as error:
            raise ApprovalDenied("conflict", "approval was decided concurrently") from error
        return decision

    def consume(self, *, project_id: str, approval_id: str, action: Mapping,
                now: datetime) -> ApprovalRequest:
        """Authorize executing ``action`` exactly once. M05 calls this before resuming."""
        request = self._get(project_id, approval_id)
        digest = action_digest(action)
        if digest != request.action_digest:
            raise ApprovalDenied("digest_mismatch", "action changed; a new approval is required")
        if request.status != "approved":
            raise ApprovalDenied("not_approved", f"approval is {request.status}")
        if now >= _parse(request.expires_at):
            raise ApprovalDenied("expired", "approval expired before use")
        if request.policy_version != self._policy_version:
            raise ApprovalDenied("policy_changed", "policy changed since approval")
        try:
            return self._store.consume(project_id, approval_id, digest)
        except Conflict as error:
            raise ApprovalDenied("conflict", "approval was already consumed") from error
