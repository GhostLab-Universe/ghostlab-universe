"""Application acceptance over real routes, stores, worlds, approval and replay.

Proposals are explicitly scripted; no inference provider is contacted. Browser acceptance
lives in web/tests/browser/application.spec.ts and runs in its own CI job. These replace
four API placeholders; the two UI placeholders have moved to executable browser tests.
"""
import dataclasses
import hashlib
import json
import time
import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from ghostlab.api import create_app
from ghostlab.api.local import local_config
from ghostlab.api.sessions import LocalDevSessions, TokenSessions
from ghostlab.contracts import Principal
from ghostlab.policy import ApprovalService, ProjectApproverAllowlist
from ghostlab.security.redaction import Redactor

PROJECT = "acceptance-a"
OWNER = Principal(principal_id="acceptance-owner", kind="human")
REVIEWER = Principal(principal_id="acceptance-reviewer", kind="human")
OTHER = Principal(principal_id="acceptance-outsider", kind="human")
CANARY = "synthetic-provider-canary-9123456789"


@pytest.fixture
def application(tmp_path):
    local = LocalDevSessions(OWNER, PROJECT)
    config = local_config(tmp_path, local, bind_host="127.0.0.1", scripted=True)
    authority = ProjectApproverAllowlist({PROJECT: {REVIEWER.principal_id}})
    services = dataclasses.replace(config.services, approvals=ApprovalService(
        config.services.approval_store, approvers=authority))
    config = dataclasses.replace(config, services=services, approvers=authority,
        sessions=TokenSessions({"owner": (OWNER, PROJECT), "reviewer": (REVIEWER, PROJECT),
                               "other": (OTHER, "acceptance-b")}), redactor=Redactor([CANARY]))
    with TestClient(create_app(config), base_url="http://127.0.0.1", client=("127.0.0.1", 51000)) as client:
        client.headers["Authorization"] = "Bearer owner"
        yield client, config


def spec(world):
    return {"schema_version": 1, "world_version": world, "objective": "Find a synthetic workflow failure",
            "success_criteria": ["Failure and approved repair reproduce"], "budget": {
                "max_model_calls": 5, "max_tool_calls": 100, "max_input_tokens": 100000,
                "max_output_tokens": 20000, "max_cost_minor": None, "cost_currency": None,
                "max_wall_clock_ms": 120000, "max_parallel_runs": 2}}


def wait(client, campaign, status):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = client.get(f"/v1/campaigns/{campaign}")
        assert response.status_code == 200, response.text
        view = response.json()
        if view["record"]["status"] == status and view["job"]["state"] != "active":
            return view
        if view["job"]["error"]:
            raise AssertionError(view["job"]["error"])
        time.sleep(.02)
    raise AssertionError(f"Campaign did not reach {status}: {view}")


def start(client, world, *, scenario="default"):
    created = client.post("/v1/campaigns", json=spec(world), params={"scripted_scenario": scenario},
                          headers={"Idempotency-Key": world.replace("/", "-") + scenario})
    assert created.status_code == 202, created.text
    campaign = created.json()["record"]["campaign_id"]
    return campaign, wait(client, campaign, "awaiting_approval")


def export_replay(client, base, run):
    exported = client.post(f"{base}/capsules/{run}")
    assert exported.status_code == 201, exported.text
    ref = exported.json()["artifact"]
    downloaded = client.get("/v1/artifacts/" + ref["id"])
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == ref["sha256"]
    replay = client.post(f"{base}/capsules/{run}/replay")
    assert replay.status_code == 200, replay.text
    assert replay.json()["reproduced"] is True and replay.json()["model_calls"] == 0
    assert replay.json()["run_status"] == "completed"
    return ref, replay.json()


def flow(application, world, *, scenario="default"):
    client, _ = application
    session = client.get("/v1/session").json()
    assert session["project_id"] == PROJECT and session["principal"] == OWNER.model_dump(mode="json")
    assert session["production_authenticated"] is False and session["inference"] == "scripted"
    campaign, paused = start(client, world, scenario=scenario)
    exp = paused["experiments"][0]["experiment_id"]
    base = f"/v1/campaigns/{campaign}/experiments/{exp}"
    diff = client.get(f"{base}/diff").json()
    assert diff["coverage"]["complete"] is True
    fork = client.get(f"{base}/runs/fork").json()
    if world == "group-journey/1":
        assert fork["execution"]["mode"] == "lab"
    assert any(not result for result in fork["result"]["checks"].values())
    export_replay(client, base, "fork")
    approval = paused["approval"]
    review = client.get("/v1/approvals/" + approval["approval_id"],
                        headers={"Authorization": "Bearer reviewer"})
    assert review.status_code == 200 and review.json()["payload_artifact_sha256"]
    body = {"schema_version": 1, "approval_id": approval["approval_id"],
            "action_digest": approval["action_digest"], "decision": "approve", "reason": None}
    decision = client.post("/v1/approvals/" + approval["approval_id"] + "/decision", json=body,
                           headers={"Authorization": "Bearer reviewer", "Idempotency-Key": "review"})
    assert decision.status_code == 200 and decision.json()["resumed"] is True
    done = wait(client, campaign, "completed")
    assert done["outcome"] == "repair_verified" and done["repair"]["verified"] is True
    repaired = client.get(f"{base}/runs/repaired").json()
    assert all(repaired["result"]["checks"].values())
    if world == "group-journey/1":
        assert repaired["execution"] == {"mode": "enforced", "guarded": True}
    else:
        assert repaired["result"]["adapter_id"] == "repaired"
    calls = done["record"]["usage"]["model_calls"]
    _, replay = export_replay(client, base, "repaired")
    assert client.get(f"/v1/campaigns/{campaign}").json()["record"]["usage"]["model_calls"] == calls
    return diff, done, replay


def test_refund_campaign_end_to_end_through_the_api(application):
    diff, done, replay = flow(application, "refund-world/1")
    assert diff["diff"]["first_divergence"]["event_id"] == 2
    assert diff["diff"]["baseline_metrics"]["refunded_minor"] == 250000
    assert diff["diff"]["faulted_metrics"]["refunded_minor"] == 500000
    assert done["repair"]["refunded_minor"] == replay["metrics"]["refunded_minor"] == 250000


@pytest.mark.parametrize("scenario,held_by", [("default", "adapter"), ("revocation_race", "guarded_boundary")])
def test_journey_campaign_end_to_end_through_the_api(application, scenario, held_by):
    diff, done, replay = flow(application, "group-journey/1", scenario=scenario)
    assert "delivered_messages_permitted" in diff["diff"]["violated_invariants"]
    assert done["repair"]["held_by"] == held_by and done["repair"]["lost_faults"] == []
    assert replay["guarded"] is True


def test_api_session_derives_identity_and_cannot_read_another_project(application):
    client, _ = application
    campaign, paused = start(client, "refund-world/1")
    exp = paused["experiments"][0]["experiment_id"]
    base = f"/v1/campaigns/{campaign}/experiments/{exp}"
    ref, _ = export_replay(client, base, "fork")
    stranger = {"Authorization": "Bearer other"}
    for path in (f"/v1/campaigns/{campaign}", base, f"{base}/runs/fork", f"{base}/diff",
                 "/v1/approvals/" + paused["approval"]["approval_id"], "/v1/artifacts/" + ref["id"]):
        denied = client.get(path, headers=stranger)
        assert denied.status_code == 404
        assert CANARY not in denied.text
    assert client.post(f"/v1/campaigns/{campaign}/resume", headers=stranger).status_code == 404
    assert client.post("/v1/capsules/" + ref["id"] + "/replay", headers=stranger).status_code == 404
    session = client.get("/v1/session", headers=stranger).json()
    assert session["project_id"] == "acceptance-b" and session["principal"]["principal_id"] == OTHER.principal_id


def test_api_errors_and_logs_hold_no_secret(application, caplog):
    client, _ = application
    body = spec("refund-world/1")
    body["objective"] = "Accidental credential " + CANARY
    refused = client.post("/v1/campaigns", json=body, headers={"Idempotency-Key": "secret"})
    assert refused.status_code == 422 and refused.json()["reason"] == "secret_shaped_text"
    assert CANARY not in refused.text and CANARY not in caplog.text
    anonymous = client.get("/v1/session", headers={"Authorization": "Bearer " + CANARY})
    assert anonymous.status_code == 401
    assert CANARY not in anonymous.text and CANARY not in caplog.text


def test_frontend_contract_matches_the_actual_service_contract():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    api = root / "src/ghostlab/api/openapi.v1.json"
    browser = root / "web/src/api/openapi.v1.json"
    assert api.read_bytes().replace(b"\r\n", b"\n") == browser.read_bytes().replace(b"\r\n", b"\n"), (
        "Refresh and regenerate the frontend client from the actual service OpenAPI")
