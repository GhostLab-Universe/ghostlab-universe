"""Authentication, authorization, isolation, bounded input and what never leaves the server.

Inference is a labeled fixture (httpx.MockTransport). Everything else is the real code.
"""

import asyncio
import dataclasses
import json
import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from conftest import LOOPBACK, Api, build_app, build_lab, spec_json
from ghostlab.api import create_app
from ghostlab.api.middleware import MAX_BODY_BYTES, _read_body
from ghostlab.api.openapi import OPENAPI_PATH
from ghostlab.api.plane import ApiConfig
from ghostlab.api.sessions import (LocalDevSessions, TokenSessions, require_local_deployment)
from ghostlab.contracts import Principal
from ghostlab.core import Fixture
from ghostlab.orchestration import CampaignController
from ghostlab.policy import (ApprovalService, ProjectApproverAllowlist, StaticPrincipalBinding,
                             StaticWorldBinding)
from ghostlab.security.local_dev import LOCAL_DEV_PRINCIPAL_ID, LocalDevRefused
from m05_harness import PROJECT, SECRET, Script, proposal
from m05_journey_harness import J, ORGANIZER, journey_reply

REFUND, RELAY = "refund-world/1", "service-relay/1"


def operations():
    document = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    for path, item in document["paths"].items():
        for method in item:
            # Login initiation/callback and provider availability must work before
            # sign-in. Their exact-path boundary is covered in test_dashboard_auth.
            if method == "get" and path in {"/v1/auth/status", "/v1/auth/{provider}/start", "/v1/auth/{provider}/callback"}:
                continue
            filled = path
            for name, value in (("campaign_id", "c-1"), ("experiment_id", "e-1"),
                                ("artifact_id", "a-1"), ("approval_id", "a-1"),
                                ("capsule_id", "a-1"), ("run", "fork")):
                filled = filled.replace(f"{{{name}}}", value)
            yield method.upper(), filled


@pytest.mark.parametrize("method,path", sorted(operations()))
def test_every_route_refuses_a_missing_or_wrong_session(stack, method, path):
    api = stack()
    for headers in ({}, {"Authorization": "Bearer nobody"}, {"Authorization": "Basic operator"},
                    {"Authorization": "Bearer "}):
        response = api.call(method, path, who=None, headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"
        assert response.headers["www-authenticate"] == "Bearer"


def test_a_session_is_required_before_a_body_is_even_parsed(stack):
    api = stack()
    response = api.post("/v1/campaigns", who=None, content=b"{not json")
    assert response.status_code == 401


def test_unknown_paths_and_documentation_routes_are_not_open(stack):
    api = stack()
    for path in ("/", "/docs", "/redoc", "/openapi.json", "/v1/nope", "/health"):
        assert api.get(path, who=None).status_code == 401
    assert api.get("/v1/nope").status_code == 404
    assert api.get("/v1/nope").json()["error"]["code"] == "not_found"
    assert api.call("DELETE", "/v1/session").status_code == 405


def test_an_unconfigured_app_denies_everything():
    with TestClient(create_app(), **LOOPBACK) as client:
        assert client.get("/v1/session").status_code == 401


def test_session_reports_who_the_server_resolved(stack):
    api = stack()
    session = api.get("/v1/session").json()
    assert session == {
        "schema_version": 1, "principal": {"principal_id": "operator-1", "kind": "human"},
        "project_id": PROJECT, "auth_mode": "configured_tokens",
        "production_authenticated": False, "can_approve": False, "inference": "fixture",
        "runnable_worlds": ["group-journey/1", "refund-world/1"]}
    assert api.get("/v1/session", who="reviewer").json()["can_approve"] is True


# -- forged scope ---------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("project_id", "project-other"), ("principal", {"principal_id": "x", "kind": "human"}),
    ("role", "supervisor"), ("scope", {"allowed_tools": ["issue_refund"]}),
    ("campaign_id", "chosen"), ("created_by", "x")])
def test_a_body_cannot_carry_identity_or_authority(stack, field, value):
    api = stack(proposal())
    body = {**spec_json(), field: value}
    response = api.post("/v1/campaigns", key="forged", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"
    assert api.script.requests == []


def test_headers_and_queries_cannot_choose_a_project(stack):
    api = stack(proposal())
    forged = {"X-Project-Id": "project-other", "X-Ghostlab-Project": "project-other",
              "X-Principal": "reviewer-1", "X-Role": "supervisor"}
    created = api.post("/v1/campaigns", key="k", json=spec_json(), headers=forged,
                       params={"project_id": "project-other", "principal": "reviewer-1"})
    assert created.status_code == 202
    assert created.json()["record"]["project_id"] == PROJECT
    assert created.json()["record"]["created_by"]["principal_id"] == "operator-1"
    session = api.get("/v1/session", headers=forged).json()
    assert session["project_id"] == PROJECT and session["can_approve"] is False


def test_a_decision_body_cannot_name_its_approver(stack):
    api = stack(proposal())
    paused = api.paused()
    approval = paused["approval"]
    body = {"schema_version": 1, "approval_id": approval["approval_id"],
            "action_digest": approval["action_digest"], "decision": "approve", "reason": None,
            "approver": {"principal_id": "reviewer-1", "kind": "human"}}
    response = api.post(f"/v1/approvals/{approval['approval_id']}/decision", key="k", json=body)
    assert response.status_code == 422
    assert api.get(f"/v1/approvals/{approval['approval_id']}").json()["decision"] is None


# -- isolation ------------------------------------------------------------------------

def owned_paths(campaign_id, experiment_id):
    base = f"/v1/campaigns/{campaign_id}"
    run = f"{base}/experiments/{experiment_id}"
    return [("GET", base), ("GET", f"{base}/events"), ("POST", f"{base}/cancel"),
            ("POST", f"{base}/resume"), ("GET", run), ("GET", f"{run}/diff"),
            ("GET", f"{run}/runs/fork"), ("POST", f"{run}/capsules/fork"),
            ("POST", f"{run}/capsules/fork/replay")]


@pytest.mark.parametrize("who", ["outsider", "stranger", "reviewer"])
def test_campaigns_read_as_absent_outside_their_owner(stack, who):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    experiment_id = paused["experiments"][0]["experiment_id"]
    for method, path in owned_paths(campaign_id, experiment_id):
        absent = api.call(method, path.replace(campaign_id, "c-missing"), who=who)
        response = api.call(method, path, who=who)
        assert response.status_code == 404, path
        assert response.json() == absent.json(), "must not tell absent from someone else's"
    assert api.view(campaign_id)["record"]["status"] == "awaiting_approval"


def test_artifacts_are_project_scoped(stack):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    experiment_id = paused["experiments"][0]["experiment_id"]
    run = api.get(f"/v1/campaigns/{campaign_id}/experiments/{experiment_id}/runs/fork").json()
    artifact_id = run["artifact"]["id"]

    mine = api.get(f"/v1/artifacts/{artifact_id}")
    assert mine.status_code == 200
    assert 'attachment; filename="' in mine.headers["content-disposition"]
    assert mine.headers["x-content-type-options"] == "nosniff"
    assert json.loads(mine.content)["actor_id"] == "synthetic-customer-1"

    assert api.get(f"/v1/artifacts/{artifact_id}", who="reviewer").status_code == 200
    theirs = api.get(f"/v1/artifacts/{artifact_id}", who="outsider")
    assert theirs.status_code == 404
    assert theirs.json() == api.get("/v1/artifacts/a-missing", who="outsider").json()


def test_a_run_artifact_is_not_a_capsule(stack):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    experiment_id = paused["experiments"][0]["experiment_id"]
    run = api.get(f"/v1/campaigns/{campaign_id}/experiments/{experiment_id}/runs/fork").json()
    response = api.post(f"/v1/capsules/{run['artifact']['id']}/replay")
    assert response.status_code == 422
    assert response.json()["reason"] == "not_a_capsule"


def test_approvals_are_visible_to_owner_and_approvers_only(stack):
    api = stack(proposal())
    paused = api.paused()
    url = f"/v1/approvals/{paused['approval']['approval_id']}"
    assert api.get(url).status_code == 200
    reviewer = api.get(url, who="reviewer").json()
    assert reviewer["request"]["summary"] and reviewer["request"]["material_risks"]
    assert api.get(url, who="stranger").status_code == 404
    assert api.get(url, who="outsider").status_code == 404
    assert api.get("/v1/approvals/approval-missing").status_code == 404


@pytest.mark.parametrize("who,status,reason", [
    ("stranger", 403, "approver_not_authorized"), ("operator", 403, "approver_not_authorized"),
    ("service", 403, "approver_not_allowed"), ("outsider", 404, "not_found")])
def test_only_an_authorized_approver_in_the_project_can_decide(stack, who, status, reason):
    api = stack(proposal())
    paused = api.paused()
    response = api.decide(paused, who=who)
    assert (response.status_code, response.json()["reason"]) == (status, reason)
    view = api.view(paused["record"]["campaign_id"])
    assert view["record"]["status"] == "awaiting_approval"
    approval = api.get(f"/v1/approvals/{paused['approval']['approval_id']}").json()
    assert approval["request"]["status"] == "pending" and approval["decision"] is None


def test_a_decision_must_name_the_exact_action_and_the_path(stack):
    api = stack(proposal())
    paused = api.paused()
    wrong = api.decide(paused, digest="0" * 64)
    assert wrong.status_code == 409
    assert wrong.json()["reason"] == "digest_mismatch"
    assert wrong.json()["error"]["code"] == "approval_invalid"

    approval = paused["approval"]
    mismatch = api.post("/v1/approvals/other-approval/decision", who="reviewer", key="k",
                        json={"schema_version": 1, "approval_id": approval["approval_id"],
                              "action_digest": approval["action_digest"], "decision": "approve",
                              "reason": None})
    assert mismatch.status_code == 422
    current = api.get(f"/v1/approvals/{approval['approval_id']}").json()
    assert current["request"]["status"] == "pending"


def test_the_reviewer_sees_the_action_the_digest_binds(stack):
    from ghostlab.policy import action_digest

    api = stack(journey_reply())
    paused = api.paused(J)
    view = api.get(f"/v1/approvals/{paused['approval']['approval_id']}", who="reviewer").json()
    assert action_digest(view["action"]) == view["request"]["action_digest"]
    assert view["action"]["execution"] == {"mode": "enforced", "guarded": True}
    assert view["action"]["fixture_sha256"]


# -- invalid requests fail before mutation ----------------------------------------------

def test_service_relay_is_refused_before_anything_exists(stack):
    api = stack(proposal())
    response = api.create(RELAY)
    assert response.status_code == 403
    assert response.json()["reason"] == "world_not_runnable"
    assert response.json()["error"]["code"] == "forbidden"
    assert api.script.requests == []
    assert api.client.app.state.plane.jobs._jobs == {}


@pytest.mark.parametrize("body,reason", [
    (spec_json(max_model_calls=10_000), "capability_denied"),
    (spec_json(max_tool_calls=500), "capability_denied")])
def test_a_budget_over_the_ceiling_is_refused_before_it_starts(stack, body, reason):
    api = stack(proposal())
    response = api.post("/v1/campaigns", key="k", json=body)
    assert response.status_code == 403
    assert response.json()["reason"] == reason
    assert api.script.requests == []


@pytest.mark.parametrize("change", [
    {"world_version": "unknown-world/9"}, {"objective": ""}, {"success_criteria": []},
    {"schema_version": 2}, {"schema_version": True}, {"budget": {}}])
def test_invalid_specs_fail_validation(stack, change):
    api = stack(proposal())
    response = api.post("/v1/campaigns", key="k", json={**spec_json(), **change})
    assert response.status_code == 422
    assert response.json()["reason"] == "invalid_request"
    assert api.script.requests == []


def test_a_campaign_needs_a_valid_idempotency_key(stack):
    api = stack(proposal())
    assert api.post("/v1/campaigns", json=spec_json()).status_code == 422
    assert api.post("/v1/campaigns", json=spec_json(), key="not valid!").status_code == 422
    assert api.script.requests == []


def test_inference_refusal_is_typed_and_mutates_nothing(stack):
    api = stack(lab_options={"transport": False})  # a real runtime: the gate must refuse it
    response = api.create(REFUND)
    assert response.status_code == 403
    assert response.json()["reason"] == "inference_not_authorized"
    assert SECRET not in response.text
    assert api.get("/v1/session").json()["inference"] == "disabled"


def test_requests_are_size_limited_before_parsing(stack):
    api = stack(proposal())
    huge = {**spec_json(), "objective": "x" * (70 * 1024)}
    response = api.post("/v1/campaigns", key="k", json=huge)
    assert response.status_code == 413
    assert response.json()["reason"] == "too_large"

    def chunks():
        yield b"{" * (70 * 1024)

    chunked = api.post("/v1/campaigns", key="k", content=chunks())
    assert chunked.status_code == 413
    too_big = api.post("/v1/capsules/import", content=b" " * (2_100_000))
    assert too_big.status_code == 413


def test_a_declared_oversize_body_is_refused_without_being_read():
    async def unread():
        raise AssertionError("the body was read")

    scope = {"type": "http", "headers": [(b"content-length", b"70000")]}
    assert asyncio.run(_read_body(scope, unread, MAX_BODY_BYTES)) is None


def test_event_pages_are_bounded(stack):
    api = stack(proposal())
    campaign_id = api.paused()["record"]["campaign_id"]
    url = f"/v1/campaigns/{campaign_id}/events"
    assert api.get(url, params={"limit": 201}).status_code == 422
    assert api.get(url, params={"limit": 0}).status_code == 422
    assert api.get(url, params={"after": -1}).status_code == 422


def test_capsule_import_rejects_tampering_and_stores_nothing(stack):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    experiment_id = paused["experiments"][0]["experiment_id"]
    capsule_id = api.post(f"/v1/campaigns/{campaign_id}/experiments/{experiment_id}"
                          "/capsules/fork").json()["artifact"]["id"]
    capsule = json.loads(api.get(f"/v1/artifacts/{capsule_id}").content)

    changed = json.loads(json.dumps(capsule))
    changed["payload"]["run"]["final_state"]["total_refunded_minor"] = 1
    response = api.post("/v1/capsules/import", json=changed)
    assert response.status_code == 422
    assert response.json()["reason"] == "capsule_invalid"

    extra = {**capsule, "note": "x"}
    assert api.post("/v1/capsules/import", json=extra).status_code == 422
    assert api.post("/v1/capsules/import", json={"payload": {}, "sha256": "0"}).status_code == 422
    assert api.post("/v1/capsules/import", content=b"[]").status_code == 422


def test_a_tampered_capsule_with_a_fresh_checksum_is_reported_not_reproduced(stack):
    from ghostlab.contracts.world_refund import capsule_digest

    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    experiment_id = paused["experiments"][0]["experiment_id"]
    capsule_id = api.post(f"/v1/campaigns/{campaign_id}/experiments/{experiment_id}"
                          "/capsules/fork").json()["artifact"]["id"]
    capsule = json.loads(api.get(f"/v1/artifacts/{capsule_id}").content)
    payload = capsule["payload"]
    payload["run"]["final_state"]["total_refunded_minor"] = 1
    imported = api.post("/v1/capsules/import",
                        json={"payload": payload, "sha256": capsule_digest(payload)})
    assert imported.status_code == 201, imported.text
    assert imported.json()["replay"]["reproduced"] is False


def test_a_capsule_for_an_unrunnable_world_is_refused(stack):
    api = stack(journey_reply())
    paused = api.paused(J)
    campaign_id = paused["record"]["campaign_id"]
    experiment_id = paused["experiments"][0]["experiment_id"]
    capsule_id = api.post(f"/v1/campaigns/{campaign_id}/experiments/{experiment_id}"
                          "/capsules/fork").json()["artifact"]["id"]
    capsule = json.loads(api.get(f"/v1/artifacts/{capsule_id}").content)
    plane = api.client.app.state.plane
    plane.services = dataclasses.replace(plane.services, worlds={})
    response = api.post("/v1/capsules/import", json=capsule)
    assert response.status_code == 403
    assert response.json()["reason"] == "world_not_runnable"


# -- the non-production boundary --------------------------------------------------------

@pytest.mark.parametrize("client,headers,reason", [
    (("10.0.0.5", 1), {}, "not_loopback"),
    (("127.0.0.1", 1), {"Host": "evil.example"}, "not_loopback"),
    (("127.0.0.1", 1), {"Origin": "https://evil.example"}, "not_loopback"),
    (("127.0.0.1", 1), {"Origin": "null"}, "not_loopback")])
def test_non_production_sessions_serve_loopback_only(tmp_path, client, headers, reason):
    lab = build_lab(tmp_path, Script())
    with TestClient(build_app(lab), base_url="http://127.0.0.1", client=client) as http:
        response = http.get("/v1/session", headers={"Authorization": "Bearer operator", **headers})
    assert response.status_code == 403
    assert response.json()["reason"] == reason


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
def test_a_local_interface_origin_is_accepted(stack, origin):
    api = stack()
    assert api.get("/v1/session", headers={"Origin": origin}).status_code == 200


@pytest.mark.parametrize("environ,host", [
    ({"GHOSTLAB_DEPLOYMENT": "hosted"}, "127.0.0.1"), ({}, "0.0.0.0"), ({}, "192.168.1.9")])
def test_no_adapter_may_be_hosted_or_exposed(tmp_path, environ, host):
    with pytest.raises(LocalDevRefused):
        require_local_deployment(host, environ)
    lab = build_lab(tmp_path, Script())
    with pytest.raises(LocalDevRefused):
        build_app(lab, bind_host=host, environ=environ)


@pytest.mark.parametrize("environ,host", [
    ({}, "127.0.0.1"), ({"GHOSTLAB_AUTH_MODE": "token"}, "127.0.0.1"),
    ({"GHOSTLAB_AUTH_MODE": "local_dev"}, "0.0.0.0"),
    ({"GHOSTLAB_AUTH_MODE": "local_dev", "GHOSTLAB_DEPLOYMENT": "hosted"}, "127.0.0.1")])
def test_local_dev_requires_the_explicit_loopback_opt_in(environ, host):
    with pytest.raises(LocalDevRefused):
        LocalDevSessions.from_environment(host, environ=environ)


def local_dev_stack(tmp_path, *replies, local_dev_mode=True):
    sessions = LocalDevSessions.from_environment(
        "127.0.0.1", environ={"GHOSTLAB_AUTH_MODE": "local_dev"})
    approvers = ProjectApproverAllowlist({PROJECT: {LOCAL_DEV_PRINCIPAL_ID}})
    lab = build_lab(tmp_path, Script(*replies))
    lab.approvals = ApprovalService(lab.approval_store, approvers=approvers,
                                    local_dev_mode=local_dev_mode)
    lab.binding = StaticPrincipalBinding({(PROJECT, LOCAL_DEV_PRINCIPAL_ID):
                                          Fixture().customer_id})
    lab.world_binding = StaticWorldBinding({(PROJECT, LOCAL_DEV_PRINCIPAL_ID, J): ORGANIZER})
    return lab, build_app(lab, sessions=sessions, approvers=approvers)


def test_a_full_local_dev_flow_is_labelled_as_not_production(tmp_path):
    lab, app = local_dev_stack(tmp_path, journey_reply())
    with TestClient(app, **LOOPBACK) as client:
        api = Api(client, lab)
        session = api.get("/v1/session", who=None).json()
        assert session["auth_mode"] == "local_dev"
        assert session["production_authenticated"] is False
        assert session["principal"] == {"principal_id": LOCAL_DEV_PRINCIPAL_ID,
                                        "kind": "local_dev"}
        created = api.create(J, who=None)
        assert created.status_code == 202, created.text
        campaign_id = created.json()["record"]["campaign_id"]
        paused = api.wait(campaign_id, "awaiting_approval", who=None)
        assert api.decide(paused, who=None).status_code == 200
        assert api.wait(campaign_id, who=None)["outcome"] == "repair_verified"


def test_local_dev_principals_cannot_approve_outside_local_dev_mode(tmp_path):
    lab, app = local_dev_stack(tmp_path, proposal(), local_dev_mode=False)
    with TestClient(app, **LOOPBACK) as client:
        api = Api(client, lab)
        campaign_id = api.create(REFUND, who=None).json()["record"]["campaign_id"]
        paused = api.wait(campaign_id, "awaiting_approval", who=None)
        response = api.decide(paused, who=None)
        assert response.status_code == 403
        assert response.json()["reason"] == "approver_not_allowed"


def test_token_sessions_keep_no_token(tmp_path):
    principal = Principal(principal_id="p-1", kind="human")
    sessions = TokenSessions({"plain-token-value": (principal, "project")})
    assert "plain-token-value" not in repr(vars(sessions))


# -- nothing sensitive leaves ------------------------------------------------------------

def crawl(api, paused):
    campaign_id = paused["record"]["campaign_id"]
    experiment_id = paused["experiments"][0]["experiment_id"]
    base = f"/v1/campaigns/{campaign_id}"
    run = f"{base}/experiments/{experiment_id}"
    pages = [api.get(path) for path in (
        base, f"{base}/events", run, f"{run}/diff", f"{run}/runs/fork", f"{run}/runs/baseline",
        f"/v1/approvals/{paused['approval']['approval_id']}", "/v1/session")]
    exported = api.post(f"{run}/capsules/fork")
    pages += [exported, api.post(f"{run}/capsules/fork/replay"),
              api.get(f"/v1/artifacts/{exported.json()['artifact']['id']}")]
    return pages


def test_no_response_carries_the_credential_or_provider_details(stack):
    api = stack(proposal())
    paused = api.paused()
    for page in crawl(api, paused):
        assert page.status_code in (200, 201), page.text
        for needle in (SECRET, "api.meta.ai", "fixture-model", "Bearer ", "credential",
                       "limits", "task_baselines"):
            assert needle not in page.text, needle


def test_a_provider_error_echoing_the_key_is_redacted(stack):
    leak = (401, {"error": {"code": "invalid_key", "message": f"Bad key {SECRET}"}})
    api = stack(leak, leak)
    campaign_id = api.create(REFUND).json()["record"]["campaign_id"]
    done = api.wait(campaign_id, "failed")
    events = api.get(f"/v1/campaigns/{campaign_id}/events").text
    assert SECRET not in json.dumps(done) and SECRET not in events


def test_model_text_with_a_secret_is_redacted_in_every_view(stack):
    api = stack(proposal(hypothesis=f"Try the key {SECRET} on a retry"))
    paused = api.paused()
    assert SECRET not in json.dumps(paused)
    for page in crawl(api, paused)[:3]:
        assert SECRET not in page.text


def test_stored_text_is_redacted_on_the_way_out_whatever_wrote_it(stack, monkeypatch):
    # M01 scrubs what it returns, so only a secret already in the checkpoint reaches this layer.
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    experiment_id = paused["experiments"][0]["experiment_id"]
    read = CampaignController.state

    def leaky(self, campaign):
        state = read(self, campaign)
        state["events"][0]["summary"] = f"event {SECRET}"
        state["errors"] = [{"code": "internal", "message": f"error {SECRET}", "retryable": False,
                            "side_effect": "none", "ref": None}]
        state["experiments"][experiment_id]["proposal"]["hypothesis"] = f"model {SECRET}"
        return state

    monkeypatch.setattr(CampaignController, "state", leaky)
    base = f"/v1/campaigns/{campaign_id}"
    pages = {"view": api.get(base), "events": api.get(f"{base}/events"),
             "experiment": api.get(f"{base}/experiments/{experiment_id}")}
    for name, page in pages.items():
        assert page.status_code == 200, name
        assert SECRET not in page.text, name
        assert "[REDACTED]" in page.text, name


def test_validation_errors_do_not_echo_the_input(stack):
    api = stack()
    body = {**spec_json(), "objective": 7, "api_key": SECRET, "note": "private-value"}
    response = api.post("/v1/campaigns", key="k", json=body)
    assert response.status_code == 422
    assert SECRET not in response.text and "private-value" not in response.text
    assert "objective" in response.text


def test_an_unexpected_failure_is_generic(stack):
    api = stack(proposal())
    plane = api.client.app.state.plane

    def explode(*args, **kwargs):
        raise RuntimeError(f"traceback with {SECRET} and a path C:/private/thing")

    plane.session_info = explode
    response = api.get("/v1/session")
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal", "message": "Internal error",
                                         "retryable": False, "side_effect": "unknown",
                                         "ref": None}, "reason": None}


def test_the_generated_document_names_no_credential(stack):
    text = OPENAPI_PATH.read_text(encoding="utf-8")
    assert "MODEL_API_KEY" not in text and "api.meta.ai" not in text
