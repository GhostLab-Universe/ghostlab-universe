"""The committed OpenAPI document, its endpoint index and its agreement with M00."""

import json
from pathlib import Path

import pytest

from ghostlab.api import create_app
from ghostlab.api.openapi import OPENAPI_PATH, document, drifted
from ghostlab.contracts.registry import CONTRACTS
from m05_harness import proposal
from m05_journey_harness import journey_reply

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas" / "v1"

ENDPOINTS = {
    ("get", "/v1/session"),
    ("post", "/v1/campaigns"),
    ("get", "/v1/campaigns/{campaign_id}"),
    ("get", "/v1/campaigns/{campaign_id}/events"),
    ("post", "/v1/campaigns/{campaign_id}/cancel"),
    ("post", "/v1/campaigns/{campaign_id}/resume"),
    ("get", "/v1/campaigns/{campaign_id}/experiments/{experiment_id}"),
    ("get", "/v1/campaigns/{campaign_id}/experiments/{experiment_id}/diff"),
    ("get", "/v1/campaigns/{campaign_id}/experiments/{experiment_id}/runs/{run}"),
    ("post", "/v1/campaigns/{campaign_id}/experiments/{experiment_id}/capsules/{run}"),
    ("post", "/v1/campaigns/{campaign_id}/experiments/{experiment_id}/capsules/{run}/replay"),
    ("post", "/v1/capsules/import"),
    ("post", "/v1/capsules/{capsule_id}/replay"),
    ("get", "/v1/artifacts/{artifact_id}"),
    ("get", "/v1/approvals/{approval_id}"),
    ("post", "/v1/approvals/{approval_id}/decision"),
}


def spec():
    return json.loads(document())


def test_the_committed_document_has_not_drifted():
    assert not drifted(), "run python -m ghostlab.api.openapi"
    assert json.loads(OPENAPI_PATH.read_text(encoding="utf-8")) == spec()


def test_the_document_is_deterministic():
    assert document() == document()


def test_the_endpoint_index_is_exact():
    found = {(method, path) for path, item in spec()["paths"].items() for method in item}
    assert found == ENDPOINTS


def test_every_operation_has_a_stable_id_and_a_typed_error():
    for path, item in spec()["paths"].items():
        for method, operation in item.items():
            assert operation["operationId"], (method, path)
            for status in ("401", "403", "404", "422"):
                body = operation["responses"][status]["content"]["application/json"]
                assert body["schema"]["$ref"].endswith("/ErrorResponse"), (method, path, status)


def test_no_request_can_name_a_project_principal_role_or_scope():
    document_spec = spec()
    forbidden = {"project_id", "principal", "principal_id", "role", "scope"}
    for path, item in document_spec["paths"].items():
        assert "project" not in path
        for operation in item.values():
            names = {p["name"].lower() for p in operation.get("parameters", [])}
            assert not names & forbidden, path
    for name in ("WorldCampaignSpec", "ApprovalSubmission"):
        assert not set(document_spec["components"]["schemas"][name]["properties"]) & forbidden


def published():
    for contract in CONTRACTS:
        path = SCHEMAS / f"{contract.name}.schema.json"
        if path.exists():
            yield contract, json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", [
    "WorldCampaignSpec", "WorldCampaignRecord", "WorldRunResult", "WorldRealityDiff",
    "WorldInvariantFinding", "WorldExperimentProposal", "CapsuleV2", "CapsuleV1",
    "ApprovalRequest", "ApprovalDecision", "ApprovalSubmission", "ExperimentEvent", "ErrorInfo",
    "TaskSpec", "Ref", "Principal", "Coverage", "RealityDiffView"])
def test_shared_contracts_appear_under_their_m00_names(name):
    assert name in spec()["components"]["schemas"]


def test_openapi_components_agree_with_the_published_m00_schemas():
    components = spec()["components"]["schemas"]
    compared = 0
    for contract, schema in published():
        component = components.get(contract.model.__name__)
        if component is None:
            continue
        compared += 1
        assert set(component["properties"]) == set(schema["properties"]), contract.name
        assert set(component.get("required", [])) == set(schema.get("required", [])), contract.name
        assert component.get("additionalProperties") == schema.get("additionalProperties")
    assert compared >= 10


def test_a_body_model_rejects_unknown_fields_in_the_published_shape():
    # What keeps a forged project_id or role out of a body: the schema says so too.
    for name in ("WorldCampaignSpec", "ApprovalSubmission", "ErrorResponse", "CampaignView"):
        assert spec()["components"]["schemas"][name]["additionalProperties"] is False


def test_the_app_builds_without_storage_or_environment(monkeypatch):
    for variable in ("GHOSTLAB_AUTH_MODE", "GHOSTLAB_DEPLOYMENT"):
        monkeypatch.delenv(variable, raising=False)
    assert create_app().openapi()["info"]["title"] == "GhostLab Control Plane"


def m00_example(name):
    path = SCHEMAS.parents[1] / "examples" / "contracts" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("example,reply", [
    ("campaign_spec.refund_timeout", proposal), ("world_campaign_spec.group_journey", journey_reply)])
def test_a_published_m00_spec_is_accepted_by_the_real_route(stack, example, reply):
    api = stack(reply())
    response = api.post("/v1/campaigns", key="example", json=m00_example(example))
    assert response.status_code == 202, response.text
    view = api.wait(response.json()["record"]["campaign_id"], "awaiting_approval")
    assert view["record"]["spec"] == m00_example(example)


def test_a_published_m00_submission_reaches_the_decision_route_unchanged(stack):
    api = stack(proposal())
    paused = api.paused()
    example = m00_example("approval_submission.approve")
    approval = paused["approval"]
    body = {**example, "approval_id": approval["approval_id"],
            "action_digest": approval["action_digest"]}
    response = api.post(f"/v1/approvals/{approval['approval_id']}/decision", who="reviewer",
                        key="example", json=body)
    assert response.status_code == 200, response.text
    assert set(response.json()["decision"]) == set(m00_example("approval_decision.approved"))


def test_the_view_shapes_match_the_published_record_examples(stack):
    api = stack(journey_reply())
    view = api.paused("group-journey/1")
    assert set(view["record"]) == set(m00_example("world_campaign_record.group_journey"))
