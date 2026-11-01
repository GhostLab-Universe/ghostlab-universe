"""HTTP routes (``ghostlab-api/1``): thin adapters over ``ControlPlane``.

No route takes a project, principal, role or scope. The session comes from the
authentication middleware, which runs before routing, so a route can't be left
unauthenticated by forgetting a dependency.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, Security
from fastapi.security import HTTPBearer

from ghostlab.contracts import ApprovalSubmission, CapsuleV1
from ghostlab.contracts.primitives import Identifier
from ghostlab.contracts.world import CapsuleV2, WorldCampaignSpec

from .scripted import DEFAULT_SCENARIO, ScriptedScenario
from .models import (ApprovalView, CampaignView, CapsuleImport, CapsuleInfo, DecisionResult,
                     ErrorResponse, EventPage, ExperimentView, RealityDiffView, ReplayResult,
                     RunName, RunView, SessionInfo)

MAX_EVENT_PAGE = 200

IdempotencyKey = Annotated[Identifier, Header(
    alias="Idempotency-Key",
    description="Client-generated identity of this action. A retry with the same key and "
                "body returns the same result and performs no second action.")]
CampaignId = Annotated[Identifier, Path()]
ExperimentId = Annotated[Identifier, Path()]
ArtifactId = Annotated[Identifier, Path()]
ApprovalId = Annotated[Identifier, Path()]

STRING_HEADER = {"schema": {"type": "string"}}
COMMON_ERRORS = {
    401: {"model": ErrorResponse, "description": "No valid session",
          "headers": {"WWW-Authenticate": STRING_HEADER}},
    403: {"model": ErrorResponse, "description": "Refused by policy"},
    404: {"model": ErrorResponse, "description": "Absent, or owned by another project"},
    413: {"model": ErrorResponse, "description": "Request body too large"},
    422: {"model": ErrorResponse, "description": "Invalid request; nothing was changed"},
    500: {"model": ErrorResponse, "description": "Unexpected failure; detail is not disclosed"},
}
WRITE_ERRORS = {
    409: {"model": ErrorResponse, "description": "Conflicts with the current state"},
    429: {"model": ErrorResponse, "description": "Too many campaigns running; retry later",
          "headers": {"Retry-After": STRING_HEADER}},
    503: {"model": ErrorResponse, "description": "The server is shutting down; retry later"},
}
REPLAYED = {"headers": {"Idempotency-Replayed": {
    "description": "Present and true when this request's identity was already used",
    **STRING_HEADER}}}

bearer = HTTPBearer(auto_error=False, description=(
    "Configured bearer tokens. In local_dev mode the session is the single local principal "
    "and no token is needed. An explicitly configured external IdP can validate tokens "
    "by introspection; the service remains loopback-only until deployment is qualified."))
router = APIRouter(prefix="/v1", responses=COMMON_ERRORS, dependencies=[Security(bearer)])


def plane(request: Request):
    return request.app.state.plane


def session(request: Request):
    return request.state.session


Plane = Annotated[object, Depends(plane)]
Session = Annotated[object, Depends(session)]


@router.get("/session", response_model=SessionInfo, operation_id="getSession", tags=["session"])
def get_session(control: Plane, who: Session):
    return control.session_info(who)


@router.post("/campaigns", response_model=CampaignView, status_code=202,
             operation_id="createCampaign", tags=["campaigns"],
             responses={**WRITE_ERRORS, 202: REPLAYED})
def create_campaign(spec: WorldCampaignSpec, key: IdempotencyKey, response: Response,
                    control: Plane, who: Session,
                    scripted_scenario: Annotated[ScriptedScenario, Query(
                        description="Scripted servers only: which checked-in proposal to use. "
                                    "Not every world has every scenario.")] = DEFAULT_SCENARIO):
    """Start a campaign in the session's project.

    The campaign id is derived from the session and the key, so a retry finds the same
    campaign and its current state instead of starting another. Reusing a key with a
    different spec is a 409. The world, budget and inference gates run before anything
    is stored. ``service-relay/1`` is a registered design, not a runnable world: 403.
    Text shaped like a credential is refused (422), not stored. ``scripted_scenario``
    selects a checked-in proposal on a ``--scripted`` server; anywhere else, or for a world
    without that scenario, it is a 422 with reason ``scripted_scenario_unavailable``. A key
    reused with another scenario is a 409, as for another spec.
    """
    view, replayed = control.create_campaign(who, spec, key, scripted_scenario)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return view


@router.get("/campaigns/{campaign_id}", response_model=CampaignView,
            operation_id="getCampaign", tags=["campaigns"])
def get_campaign(campaign_id: CampaignId, control: Plane, who: Session):
    """Authoritative status from the checkpoint; use this after any reconnect."""
    return control.campaign_view(who, campaign_id)


@router.get("/campaigns/{campaign_id}/events", response_model=EventPage,
            operation_id="listCampaignEvents", tags=["campaigns"])
def list_events(campaign_id: CampaignId, control: Plane, who: Session,
                after: Annotated[int, Query(ge=0, le=10**12)] = 0,
                limit: Annotated[int, Query(ge=1, le=MAX_EVENT_PAGE)] = 100):
    """Poll application events after a sequence number. Reconnecting loses nothing."""
    return control.events(who, campaign_id, after, limit)


@router.post("/campaigns/{campaign_id}/cancel", response_model=CampaignView, status_code=202,
             operation_id="cancelCampaign", tags=["campaigns"], responses=WRITE_ERRORS)
def cancel_campaign(campaign_id: CampaignId, control: Plane, who: Session):
    """Stop a running, paused or stalled campaign at its next guard. Repeating is harmless.

    A 202 means it stopped, or a worker has the request: read ``job.cancel_requested`` and
    the status. A 409 with reason ``cancel_incomplete`` means the campaign could not be
    stopped because its interrupted step can't finish; it is unchanged, and ``job.error``
    says why. A finished campaign is a 409 with reason ``campaign_finished``, unless it was
    already cancelled. An approval not yet used stays unspent and can't run anything.
    """
    return control.cancel(who, campaign_id)


@router.post("/campaigns/{campaign_id}/resume", response_model=CampaignView, status_code=202,
             operation_id="resumeCampaign", tags=["campaigns"], responses=WRITE_ERRORS)
def resume_campaign(campaign_id: CampaignId, control: Plane, who: Session):
    """Owner only. Re-check a paused campaign, or continue a stalled one.

    This grants nothing: a pending approval pauses again, and a rejected, expired or
    used one ends the campaign. A decision already resumes the campaign, so this is
    for retrying after ``settings_changed`` or a stall. A finished campaign is a 409 with
    reason ``campaign_finished``, because there is nothing to resume.
    """
    return control.resume(who, campaign_id)


@router.get("/campaigns/{campaign_id}/experiments/{experiment_id}",
            response_model=ExperimentView, operation_id="getExperiment", tags=["evidence"])
def get_experiment(campaign_id: CampaignId, experiment_id: ExperimentId, control: Plane,
                   who: Session):
    return control.experiment(who, campaign_id, experiment_id)


@router.get("/campaigns/{campaign_id}/experiments/{experiment_id}/diff",
            response_model=RealityDiffView, operation_id="getRealityDiff", tags=["evidence"])
def get_diff(campaign_id: CampaignId, experiment_id: ExperimentId, control: Plane,
             who: Session):
    """Baseline against fork, from the recorded evidence, with the experiment's coverage."""
    return control.diff(who, campaign_id, experiment_id)


@router.get("/campaigns/{campaign_id}/experiments/{experiment_id}/runs/{run}",
            response_model=RunView, operation_id="getRun", tags=["evidence"])
def get_run(campaign_id: CampaignId, experiment_id: ExperimentId, run: RunName, control: Plane,
            who: Session):
    return control.run(who, campaign_id, experiment_id, run)


@router.post("/campaigns/{campaign_id}/experiments/{experiment_id}/capsules/{run}",
             response_model=CapsuleInfo, status_code=201, operation_id="exportCapsule",
             tags=["capsules"], responses=WRITE_ERRORS)
def export_capsule(campaign_id: CampaignId, experiment_id: ExperimentId, run: RunName,
                   control: Plane, who: Session):
    """Store a Failure Capsule for a recorded run. Content-addressed, so a retry is the same."""
    return control.export_capsule(who, campaign_id, experiment_id, run)


@router.post("/campaigns/{campaign_id}/experiments/{experiment_id}/capsules/{run}/replay",
             response_model=ReplayResult, operation_id="replayCampaignCapsule",
             tags=["capsules"])
def replay_campaign_capsule(campaign_id: CampaignId, experiment_id: ExperimentId, run: RunName,
                            control: Plane, who: Session):
    """Replay an exported capsule's recorded actions. Makes no model call."""
    return control.replay_campaign_capsule(who, campaign_id, experiment_id, run)


@router.post("/capsules/import", response_model=CapsuleImport, status_code=201,
             operation_id="importCapsule", tags=["capsules"], responses=WRITE_ERRORS)
def import_capsule(capsule: CapsuleV1 | CapsuleV2, control: Plane, who: Session):
    """Validate a capsule, replay it, and store it in the session's project.

    An imported capsule is untrusted data. It is identified by its checksum, so a retry
    is the same. A capsule that doesn't reproduce is stored and reported as not reproduced.
    One that claims what its adapter doesn't do (422 ``capsule_invalid``), or holds text
    shaped like a credential (422 ``secret_shaped_text``), is refused and not stored.
    """
    return control.import_capsule(who, capsule)


@router.post("/capsules/{capsule_id}/replay", response_model=ReplayResult,
             operation_id="replayCapsule", tags=["capsules"])
def replay_capsule(capsule_id: ArtifactId, control: Plane, who: Session):
    """Replay any capsule stored in this project. Makes no model call."""
    return control.replay_capsule(who, capsule_id)


@router.get("/artifacts/{artifact_id}", operation_id="downloadArtifact", tags=["evidence"],
            response_class=Response,
            responses={200: {"content": {"application/json": {"schema": {}}},
                             "description": "Artifact bytes, verified against their digest",
                             "headers": {"ETag": STRING_HEADER,
                                         "Content-Disposition": STRING_HEADER}}})
def download_artifact(artifact_id: ArtifactId, control: Plane, who: Session):
    """Download an artifact owned by the session's project."""
    return control.artifact_response(who, artifact_id)


@router.get("/approvals/{approval_id}", response_model=ApprovalView, operation_id="getApproval",
            tags=["approvals"])
def get_approval(approval_id: ApprovalId, control: Plane, who: Session):
    """Readable by the campaign's owner and the project's approvers; otherwise 404."""
    return control.approval(who, approval_id)


@router.post("/approvals/{approval_id}/decision", response_model=DecisionResult,
             operation_id="decideApproval", tags=["approvals"],
             responses={**WRITE_ERRORS, 200: REPLAYED})
def decide_approval(approval_id: ApprovalId, submission: ApprovalSubmission,
                    key: IdempotencyKey, response: Response, control: Plane, who: Session):
    """Approve or reject through M06. The approver is the session, never the body.

    ``approval_id`` must match the path and ``action_digest`` the request, and the
    campaign then resumes under its owner. Knowing an id or digest is not authority.
    A retry with the same key returns the recorded decision.
    """
    result, replayed = control.decide(who, approval_id, submission, key)
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result
