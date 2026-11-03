"""Test doubles for M06. FakeApprovalStore follows ghostlab-storage/1 semantics:
atomic compare-and-set under one lock, project isolation, re-validated records.
It stands in for M04's SQLite store, which is implemented separately.
"""

import threading

from ghostlab.contracts import ApprovalDecision, ApprovalRequest
from ghostlab.storage.interfaces import ApprovalStore, Conflict, NotFound


class FakeApprovalStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str], ApprovalRequest] = {}
        self._decisions: dict[tuple[str, str], ApprovalDecision] = {}

    def _key(self, project_id, approval_id):
        return (project_id, approval_id)

    def _replace(self, request, status):
        return ApprovalRequest.model_validate({**request.model_dump(), "status": status})

    def create(self, request):
        with self._lock:
            key = self._key(request.scope.project_id, request.approval_id)
            if request.status != "pending" or key in self._requests:
                raise Conflict("approval exists or is not pending")
            self._requests[key] = request

    def get(self, project_id, approval_id):
        with self._lock:
            try:
                return self._requests[self._key(project_id, approval_id)]
            except KeyError:
                raise NotFound("approval not found") from None

    def get_decision(self, project_id, approval_id):
        with self._lock:
            return self._decisions.get(self._key(project_id, approval_id))

    def _transition(self, project_id, approval_id, expected, new, digest=None):
        key = self._key(project_id, approval_id)
        current = self._requests.get(key)
        if current is None:
            raise NotFound("approval not found")
        if current.status != expected or (digest is not None and digest != current.action_digest):
            raise Conflict("precondition failed")
        self._requests[key] = self._replace(current, new)
        return self._requests[key]

    def record_decision(self, project_id, decision, *, new_status):
        with self._lock:
            updated = self._transition(project_id, decision.approval_id, "pending", new_status,
                                       decision.action_digest)
            self._decisions[self._key(project_id, decision.approval_id)] = decision
            return updated

    def mark_expired(self, project_id, approval_id):
        with self._lock:
            return self._transition(project_id, approval_id, "pending", "expired")

    def consume(self, project_id, approval_id, action_digest):
        with self._lock:
            return self._transition(project_id, approval_id, "approved", "consumed", action_digest)


assert isinstance(FakeApprovalStore(), ApprovalStore)  # structural conformance


def principal(kind="human", principal_id="reviewer-1"):
    return {"principal_id": principal_id, "kind": kind}
