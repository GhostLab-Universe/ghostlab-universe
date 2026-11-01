"""Independent-review policy, additive to the explicit single-user demo policy."""

from .approvals import ApprovalDenied, ApprovalService
from ghostlab.storage.interfaces import NotFound


class SeparatedApprovalService(ApprovalService):
    """A requesting principal cannot decide its own action, including rejection.

    Keep using ApprovalService for the clearly labelled single-user demo. An
    externally authenticated configuration must use this service and supply an
    independent, project-authorized human reviewer.
    """

    def decide(self, *, session_principal, project_id, submission, decision_id, now):
        # Check authority before looking up the request, preserving non-disclosure.
        if (session_principal.kind != "human"
                or not self._approvers.may_approve(session_principal, project_id)):
            raise ApprovalDenied("approver_not_authorized", "an authorized human reviewer is required")
        request = self._get(project_id, submission.approval_id)
        if request.scope.principal.principal_id == session_principal.principal_id:
            raise ApprovalDenied("approver_not_authorized", "a separate reviewer is required")
        return super().decide(session_principal=session_principal, project_id=project_id,
                              submission=submission, decision_id=decision_id, now=now)

    def consume(self, *, project_id, approval_id, action, now):
        request = self._get(project_id, approval_id)
        if request.status == "approved":
            try:
                decision = self._store.get_decision(project_id, approval_id)
            except NotFound as error:
                raise ApprovalDenied("not_approved", "independent review evidence is missing") from error
            if (decision.approver.kind != "human"
                    or decision.approver.principal_id == request.scope.principal.principal_id
                    or not self._approvers.may_approve(decision.approver, project_id)):
                raise ApprovalDenied("approver_not_authorized", "independent review is required")
        return super().consume(project_id=project_id, approval_id=approval_id, action=action, now=now)
