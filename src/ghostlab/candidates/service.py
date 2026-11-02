"""Candidate lifecycle orchestration bound to human approval.

:class:`CandidateService` moves candidates through propose, validate,
evaluate and promotion. Authority always comes from the caller's
trusted session (``scope`` / ``project_id``), never from record
bodies: ``created_by``, ``evaluated_by``, ``rejected_by`` and
``decided_by`` are the session principal id. Promotion executes only through the existing
single-use digest-bound approval flow (``ApprovalService.consume``):
the promotion action pins the candidate, both endpoints and the
evaluation digest, so any change needs a new approval. Zero model
calls; records carry no credentials.

Needs the contracts extra (pydantic) for ``TrustedScope`` and the
approval service; the record, validation, store and harness modules
stay standard-library only.
"""

from datetime import datetime

from ..contracts import Principal, TrustedScope
from ..policy.approvals import ApprovalDenied, ApprovalService, action_digest
from .allowlist import ChangeRefusal, validate_change
from .records import (CANDIDATE_SCHEMA_VERSION, CandidateError, candidate_id,
                      check_project_id, digest_of, promotion_id,
                      validate_candidate, validate_evaluation)
from .store import CandidateStore

# The approval contract (ghostlab-contracts/1, owned elsewhere) only admits
# apply_repair, change_policy and execute_consequential_action. Promoting a
# candidate is a consequential action; candidate identity rides in the
# action body, pinned by the same digest binding.
PROMOTION_ACTION_KIND = "execute_consequential_action"


def promotion_action(*, promotion_id: str, candidate_id: str,
                     project_id: str, parent: dict, proposed: dict,
                     evaluation_digest: str) -> dict:
    """The exact promotion action bound to one approval digest."""
    return {"promotion_id": promotion_id, "candidate_id": candidate_id,
            "project_id": project_id,
            "parent": {"target_id": parent["target_id"],
                       "target_version": parent["target_version"]},
            "proposed": {"target_id": proposed["target_id"],
                         "target_version": proposed["target_version"]},
            "evaluation_digest": evaluation_digest}


class CandidateService:
    """Lifecycle operations; one registry world per service."""

    def __init__(self, store: CandidateStore, registry,
                 world_version: str,
                 approvals: ApprovalService | None = None):
        if not isinstance(store, CandidateStore):
            raise TypeError("CandidateService needs a CandidateStore")
        if type(world_version) is not str or not world_version:
            raise ValueError("invalid world_version")
        if approvals is not None and not isinstance(approvals,
                                                    ApprovalService):
            raise TypeError("approvals must be an ApprovalService")
        self._store = store
        self._registry = registry
        self._world = world_version
        self._approvals = approvals

    @property
    def store(self) -> CandidateStore:
        return self._store

    def propose(self, *, scope: TrustedScope, mode: str, parent: dict,
                proposed: dict, diff: list, evidence_refs=(),
                now_iso: str) -> dict:
        """Store a new candidate in ``proposed`` (shape-checked only).

        Registration and allowlist checks run in :meth:`validate`, so a
        proposed record is never mistaken for an accepted change.
        """
        if not isinstance(scope, TrustedScope):
            raise TypeError("scope must come from the trusted session")
        check_project_id(scope.project_id)
        if type(now_iso) is not str or not now_iso:
            raise ValueError("invalid now_iso")
        body = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "candidate_id": "pending",
            "project_id": scope.project_id,
            "mode": mode,
            "parent": parent,
            "proposed": proposed,
            "diff": diff,
            "evidence_refs": list(evidence_refs),
            "status": "proposed",
            "rejection": None,
            "created_by": scope.principal.principal_id,
            "created_at": now_iso,
            "digest": "pending",
        }
        clean = validate_candidate(body)
        stable = {k: v for k, v in clean.items()
                  if k not in ("candidate_id", "status", "rejection",
                               "created_by", "created_at", "digest")}
        clean["candidate_id"] = candidate_id(scope.project_id, stable)
        clean["digest"] = digest_of(clean)
        return self._store.save_candidate(project_id=scope.project_id,
                                          body=clean)

    def validate(self, *, scope: TrustedScope, candidate_id: str,
                 now_iso: str) -> dict:
        """Run registration/allowlist checks; ``validated`` or ``rejected``."""
        if not isinstance(scope, TrustedScope):
            raise TypeError("scope must come from the trusted session")
        if type(now_iso) is not str or not now_iso:
            raise ValueError("invalid now_iso")
        current = self._store.get_candidate(scope.project_id, candidate_id)
        if current["status"] != "proposed":
            raise CandidateError(
                f"candidate is {current['status']}, not proposed")
        try:
            validate_change(mode=current["mode"], parent=current["parent"],
                            proposed=current["proposed"], diff=current["diff"],
                            registry=self._registry,
                            world_version=self._world)
        except ChangeRefusal as refusal:
            return self._store.transition_candidate(
                scope.project_id, candidate_id, expected="proposed",
                new_status="rejected",
                rejection={"code": refusal.code, "message": str(refusal),
                           "rejected_by": scope.principal.principal_id,
                           "rejected_at": now_iso})
        return self._store.transition_candidate(
            scope.project_id, candidate_id, expected="proposed",
            new_status="validated")

    def record_evaluation(self, *, scope: TrustedScope,
                          evaluation: dict) -> dict:
        """Store a multidimensional evaluation; candidate ``validated``.

        Only evaluations produced for this candidate in this project are
        accepted; the candidate moves ``validated`` -> ``evaluated``.
        """
        if not isinstance(scope, TrustedScope):
            raise TypeError("scope must come from the trusted session")
        clean = validate_evaluation(evaluation)
        if clean["project_id"] != scope.project_id:
            raise CandidateError("evaluation names a different project")
        if clean["evaluated_by"] != scope.principal.principal_id:
            raise CandidateError("evaluation evaluator must be the session principal")
        current = self._store.get_candidate(scope.project_id,
                                            clean["candidate_id"])
        if current["status"] != "validated":
            raise CandidateError(
                f"candidate is {current['status']}, not validated")
        if clean["world_version"] != self._world:
            raise CandidateError("evaluation is for a different world")
        for side in ("parent", "proposed"):
            claimed = clean["endpoints"][side]
            pinned = current[side]
            for key in ("target_id", "target_version", "code_identity"):
                if claimed[key] != pinned[key]:
                    raise CandidateError(
                        f"evaluation {side} does not match the candidate")
            if (claimed["config_digest"] is not None
                    and claimed["config_digest"] != pinned["config_digest"]):
                raise CandidateError(
                    f"evaluation {side} config does not match the candidate")
        stored = self._store.save_evaluation(project_id=scope.project_id,
                                             body=clean)
        self._store.transition_candidate(
            scope.project_id, clean["candidate_id"], expected="validated",
            new_status="evaluated")
        return stored

    def request_promotion(self, *, scope: TrustedScope, candidate_id: str,
                          evaluation_id: str, approval_id: str,
                          campaign_id: str, task_id: str, summary: str,
                          material_risks: list, now: datetime,
                          ttl) -> dict:
        """Request human approval to promote; candidate ``promotion_requested``.

        The approval action pins the candidate, both endpoints and the
        evaluation digest. Requires an approval service on this service.
        """
        if not isinstance(scope, TrustedScope):
            raise TypeError("scope must come from the trusted session")
        if self._approvals is None:
            raise CandidateError("this service has no approval service")
        current = self._store.get_candidate(scope.project_id, candidate_id)
        if current["status"] != "evaluated":
            raise CandidateError(
                f"candidate is {current['status']}, not evaluated")
        evaluation = self._store.get_evaluation(scope.project_id,
                                                evaluation_id)
        if evaluation["candidate_id"] != candidate_id:
            raise CandidateError("evaluation is for a different candidate")
        promo_id = promotion_id(scope.project_id, candidate_id, approval_id)
        action = promotion_action(
            promotion_id=promo_id, candidate_id=candidate_id,
            project_id=scope.project_id, parent=current["parent"],
            proposed=current["proposed"],
            evaluation_digest=evaluation["digest"])
        # CAS the candidate before any side effect: concurrent losers fail
        # here with nothing leaked, and a failed insert below reverts.
        self._store.transition_candidate(
            scope.project_id, candidate_id, expected="evaluated",
            new_status="promotion_requested")
        try:
            self._approvals.request(
                scope=scope, approval_id=approval_id,
                campaign_id=campaign_id, task_id=task_id,
                action_kind=PROMOTION_ACTION_KIND, action=action,
                payload_ref_id=evaluation_id, summary=summary,
                material_risks=list(material_risks), now=now, ttl=ttl)
        except Exception:
            self._store.transition_candidate(
                scope.project_id, candidate_id,
                expected="promotion_requested", new_status="evaluated")
            raise
        promo_body = {"schema_version": CANDIDATE_SCHEMA_VERSION,
                        "promotion_id": promo_id,
                        "candidate_id": candidate_id,
                        "project_id": scope.project_id,
                        "approval_id": approval_id,
                        "action_digest": action_digest(action),
                        "status": "pending", "decided_by": None,
                        "decided_at": None, "digest": "pending"}
        promo_body["digest"] = digest_of(promo_body)
        try:
            self._store.save_promotion(project_id=scope.project_id,
                                       body=promo_body)
        except Exception:
            self._store.transition_candidate(
                scope.project_id, candidate_id,
                expected="promotion_requested", new_status="evaluated")
            raise
        return self._store.get_promotion(scope.project_id, promo_id)

    def complete_promotion(self, *, scope: TrustedScope,
                           candidate_id: str, promotion_id: str,
                           action: dict, now: datetime,
                           decided_at: str) -> dict:
        """Consume the approval once and promote; single use by digest.

        Project and decider come from the trusted session, never the
        caller. Local state is verified before the burn: a changed
        action or a non-pending promotion refuses without consuming
        the approval. On success the promotion completes and the
        candidate is ``promoted``. An expired approval marks the
        promotion expired and returns the candidate to ``evaluated``
        for a fresh request. Any other denial propagates and the
        promotion stays pending.
        """
        if not isinstance(scope, TrustedScope):
            raise TypeError("scope must come from the trusted session")
        project_id = scope.project_id
        decided_by = scope.principal.principal_id
        check_project_id(project_id)
        promotion = self._store.get_promotion(project_id, promotion_id)
        if promotion["candidate_id"] != candidate_id:
            raise CandidateError("promotion is for a different candidate")
        if promotion["status"] != "pending":
            raise CandidateError(
                f"promotion is {promotion['status']}, not pending")
        candidate = self._store.get_candidate(project_id, candidate_id)
        if candidate["status"] != "promotion_requested":
            raise CandidateError(
                f"candidate is {candidate['status']}, not promotion_requested")
        if action_digest(action) != promotion["action_digest"]:
            raise CandidateError(
                "action changed; a new approval is required")
        if self._approvals is None:
            raise CandidateError("this service has no approval service")
        try:
            self._approvals.consume(project_id=project_id,
                                    approval_id=promotion["approval_id"],
                                    action=action, now=now)
        except ApprovalDenied as denied:
            if denied.code == "expired":
                self._store.transition_promotion(
                    project_id, promotion_id, new_status="expired",
                    decided_by=decided_by, decided_at=decided_at)
                self._store.transition_candidate(
                    project_id, candidate_id,
                    expected="promotion_requested", new_status="evaluated")
                return self._store.get_promotion(project_id, promotion_id)
            raise
        self._store.transition_promotion(
            project_id, promotion_id, new_status="completed",
            decided_by=decided_by, decided_at=decided_at)
        self._store.transition_candidate(
            project_id, candidate_id, expected="promotion_requested",
            new_status="promoted")
        return self._store.get_promotion(project_id, promotion_id)

    def abandon_promotion(self, *, scope: TrustedScope, candidate_id: str,
                          promotion_id: str, now_iso: str) -> dict:
        """Drop a pending promotion after its approval was rejected.

        Requires the approval to be actually rejected: abandonment
        mirrors a human rejection, so it cannot kill a pending or
        approved promotion. The candidate returns to ``evaluated``
        so a corrected request can follow.
        """
        if not isinstance(scope, TrustedScope):
            raise TypeError("scope must come from the trusted session")
        if self._approvals is None:
            raise CandidateError("this service has no approval service")
        promotion = self._store.get_promotion(scope.project_id, promotion_id)
        if promotion["candidate_id"] != candidate_id:
            raise CandidateError("promotion is for a different candidate")
        try:
            request = self._approvals.get_request(
                project_id=scope.project_id,
                approval_id=promotion["approval_id"])
        except ApprovalDenied as denied:
            raise CandidateError(
                f"approval is unavailable: {denied}") from denied
        if request.status != "rejected":
            raise CandidateError(
                "approval is " + request.status + ", not rejected;"
                " abandonment needs a human rejection")
        self._store.transition_promotion(
            scope.project_id, promotion_id, new_status="rejected",
            decided_by=scope.principal.principal_id, decided_at=now_iso)
        self._store.transition_candidate(
            scope.project_id, candidate_id, expected="promotion_requested",
            new_status="evaluated")
        return self._store.get_promotion(scope.project_id, promotion_id)


__all__ = ["PROMOTION_ACTION_KIND", "CandidateService", "Principal",
           "promotion_action"]
