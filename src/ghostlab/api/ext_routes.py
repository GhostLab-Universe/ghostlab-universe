"""Extension routes (``ghostlab-api/1``): thin adapters over ExtensionPlane.

Library errors become fixed typed errors: unknown worlds are 404, invalid
requests are 422, oversize envelopes are 413, world refusals are 403.
Nothing echoes request input. Request bodies cross as plain JSON data.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Security

from ghostlab.core import ToolError

from .errors import ApiError, not_found
from .ext_models import (CampaignRequest, ExtCampaignReport, ExtEnvironmentDetail,
                         ExtEnvironmentList, ExtFidelityReport, ExtFixture,
                         ExtInspection, ExtMinimizeResult, ExtQualificationReport,
                         ExtReplayResult, ExtRetestResult, FidelityRequest,
                         InspectRequest, MinimizeRequest, ReplayRequest, RetestRequest,
                         QualifyRequest)
from .ext_plane import ExtBaselineNotReproduced, ExtTooLarge, ExtensionPlane
from .routes import COMMON_ERRORS, bearer

router = APIRouter(prefix="/v1/extensions", responses=COMMON_ERRORS,
                   dependencies=[Security(bearer)])


def ext_plane(request: Request):
    return request.app.state.ext_plane


def session(request: Request):
    return request.state.session


Plane = Annotated[ExtensionPlane, Depends(ext_plane)]
Session = Annotated[object, Depends(session)]


def _guard(call):
    try:
        return call()
    except ExtTooLarge:
        raise ApiError(413, "validation_failed",
                       "The request exceeds the size bound; nothing was run") from None
    except ExtBaselineNotReproduced:
        raise ApiError(422, "validation_failed",
                       "The baseline run did not show the selected failure",
                       reason="baseline_not_reproduced") from None
    except LookupError:
        raise not_found() from None
    except ToolError:
        raise ApiError(403, "forbidden", "The world refused the action") from None
    except (ValueError, TypeError):
        raise ApiError(422, "validation_failed",
                       "Invalid extension request; nothing was run") from None


def _json(request) -> dict:
    """The request body as plain JSON data (no frozen mappings)."""
    data = request.model_dump(mode="json")
    if type(data) is not dict:
        raise ValueError("Invalid request body")
    return data


@router.get("/environments", response_model=ExtEnvironmentList,
            operation_id="listExtEnvironments", tags=["extensions"])
def list_environments(control: Plane, who: Session):
    return {"environments": _guard(control.list_environments)}


@router.get("/environments/{world:path}/fixtures/{name}", response_model=ExtFixture,
            operation_id="getExtFixture", tags=["extensions"])
def get_fixture(control: Plane, who: Session, world: str, name: str):
    return _guard(lambda: control.fixture_snapshot(world, name))


@router.get("/environments/{world:path}", response_model=ExtEnvironmentDetail,
            operation_id="getExtEnvironment", tags=["extensions"])
def get_environment(control: Plane, who: Session, world: str):
    return _guard(lambda: control.describe_environment(world))


@router.post("/qualify", response_model=ExtQualificationReport,
             operation_id="qualifyExtension", tags=["extensions"])
def qualify_environment(control: Plane, who: Session, request: QualifyRequest):
    body: dict[str, Any] = _json(request)
    report = _guard(lambda: control.qualify(
        world_version=body["world_version"], fixture=body["fixture"],
        scenario=body["scenario"], script=body["script"], task_id=body["task_id"]))
    return {"world_version": report["world_version"],
            "environment_id": report["environment_id"], "passed": report["passed"],
            "checks": report["checks"],
            "missing_capabilities": report["missing_capabilities"],
            "notes": report["notes"], "details": report}


@router.post("/fidelity", response_model=ExtFidelityReport,
             operation_id="assessExtFidelity", tags=["extensions"])
def assess_fidelity(control: Plane, who: Session, request: FidelityRequest):
    body: dict[str, Any] = _json(request)
    report = _guard(lambda: control.fidelity(
        world_version=body["world_version"], fixture=body["fixture"],
        scenario=body["scenario"], actor_id=body["actor_id"], script=body["script"],
        repeats=body["repeats"], expected_trace=body["expected_trace"],
        expected_state=body["expected_state"], declared_tested=body["declared_tested"],
        declared_approximations=body["declared_approximations"],
        declared_omissions=body["declared_omissions"]))
    snapshot = report["snapshot"]
    return {"world_version": report["world_version"], "passed": report["passed"],
            "determinism_identical": report["determinism"]["identical"],
            "replay_reproduced": report["replay"]["reproduced"],
            "snapshot_supported": snapshot["supported"] if snapshot["probed"] else None,
            "missing_capabilities": report["missing_capabilities"],
            "notes": report["notes"], "details": report}


@router.post("/campaigns", response_model=ExtCampaignReport,
             operation_id="runExtCampaign", tags=["extensions"])
def run_campaign(control: Plane, who: Session, request: CampaignRequest):
    body: dict[str, Any] = _json(request)
    report = _guard(lambda: control.campaign(
        world_version=body["world_version"], attempts=body["attempts"],
        allow_tools=body["allow_tools"]))
    return {"attempts_run": report["bounds"]["attempts_run"],
            "tools_used": report["coverage"]["tools_used"],
            "faults_fired": report["coverage"]["faults_fired"],
            "rules_failed": report["coverage"]["rules_failed"],
            "caps_respected": report["bounds"]["caps_respected"],
            "guided_excluded": report["arms"]["guided"]["excluded_from_effectiveness"],
            "details": report}


@router.post("/inspect", response_model=ExtInspection,
             operation_id="inspectExtCapsule", tags=["extensions"])
def inspect_capsule(control: Plane, who: Session, request: InspectRequest):
    body: dict[str, Any] = _json(request)
    report = _guard(lambda: control.inspect(body["envelope"]))
    return {"schema_version": report["schema_version"],
            "world_version": report["world_version"],
            "replay_kind": report["replay_kind"], "truncated": report["truncated"],
            "failed_invariants": report["failed_invariants"],
            "external": [dep["name"] for dep in report["external_dependencies"]],
            "replay_supported": report["recorded_replay"]["supported"],
            "replay_detail": report["recorded_replay"]["detail"], "details": report}


@router.post("/replay", response_model=ExtReplayResult,
             operation_id="replayExtRecorded", tags=["extensions"])
def replay_recorded(control: Plane, who: Session, request: ReplayRequest):
    body: dict[str, Any] = _json(request)
    report = _guard(lambda: control.replay(
        world_version=body["world_version"], fixture=body["fixture"],
        scenario=body["scenario"], actor_id=body["actor_id"], trace=body["trace"],
        state=body["state"], checks=body["checks"]))
    return {"reproduced": report["reproduced"], "matches": report["matches"],
            "divergence": report["divergence"], "tool_calls": report["tool_calls"],
            "failed": report["failed"], "witness_sha256": report["witness_sha256"],
            "details": report}


@router.post("/retest", response_model=ExtRetestResult,
             operation_id="retestExtFresh", tags=["extensions"])
def retest_fresh(control: Plane, who: Session, request: RetestRequest):
    body: dict[str, Any] = _json(request)
    report = _guard(lambda: control.retest(
        world_version=body["world_version"], target=body["target"],
        fixture=body["fixture"], scenario=body["scenario"], actor_id=body["actor_id"],
        task=body["task"], allow_tools=body["allow_tools"], cap=body["cap"]))
    return {"status": report["status"], "tool_calls": report["tool_calls"],
            "stopped": report["stopped"], "denied": report["denied"],
            "denial_summary": report["denial_summary"],
            "target_adapter_id": report["target_adapter_id"],
            "target_adapter_version": report["target_adapter_version"],
            "failed": report["failed"], "witness_sha256": report["witness_sha256"],
            "replay_equivalence_claimed": report["replay_equivalence_claimed"],
            "details": report}


@router.post("/minimize", response_model=ExtMinimizeResult,
             operation_id="minimizeExtFaults", tags=["extensions"])
def minimize_faults(control: Plane, who: Session, request: MinimizeRequest):
    body: dict[str, Any] = _json(request)
    report = _guard(lambda: control.minimize(
        world_version=body["world_version"], target=body["target"],
        fixture=body["fixture"], scenario_template=body["scenario_template"],
        fault_keys=body["fault_keys"], actor_id=body["actor_id"],
        task=body["task"], allow_tools=body["allow_tools"], cap=body["cap"],
        invariant=body["invariant"], max_runs=body["max_runs"],
        deadline_ms=body["deadline_ms"]))
    return {"status": report["status"], "selected_faults": report["selected_faults"],
            "removed_faults": report["removed_faults"], "runs_used": report["runs_used"],
            "cardinality_minimal": report["cardinality_minimal"],
            "witness_sha256": report["failure_witness_sha256"],
            "regression": report["regression"], "details": report}
