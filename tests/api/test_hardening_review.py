"""Review repros against the real API boundary, worker and SQLite stores."""
import dataclasses
import warnings

import httpx
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from ghostlab.api import create_app
from ghostlab.api.introspection import IntrospectionSessions, IntrospectionConfig, ProjectMember
from ghostlab.api.plane import ApiConfig
from ghostlab.api.sessions import TokenSessions
from ghostlab.orchestration.inference import LiveInferenceAuthorization
from ghostlab.policy import ApprovalDenied, ProjectApproverAllowlist
from ghostlab.policy.separation import SeparatedApprovalService
from ghostlab.storage import sqlite as db
from conftest import LOOPBACK, TOKENS, Api, build_lab, spec_json
from m05_harness import OPERATOR, PROJECT, REVIEWER, Script, proposal


class CountingNetwork(httpx.BaseTransport):
    def __init__(self):
        self.calls = []

    def handle_request(self, request):
        self.calls.append(request)
        raise AssertionError("No request should reach this in-process capture")


class Wrapped:
    def __init__(self, sessions):
        self.sessions = sessions

    def authenticate(self, headers):
        return self.sessions.authenticate(headers)

    def close(self):
        self.sessions.close()


def config(lab, sessions):
    return ApiConfig(services=lab.services(), sessions=sessions,
                     approvers=lab.approvals._approvers, graph_db=lab.paths.graph_db,
                     app_db=lab.paths.app_db)


@pytest.mark.parametrize("auth_kind", ["tokens", "introspection", "wrapped-introspection"])
def test_every_live_api_path_is_refused_regardless_of_session_type_or_content_label(tmp_path, auth_kind):
    wire = CountingNetwork()
    lab = build_lab(tmp_path, Script(), real_transport=wire, live=LiveInferenceAuthorization(
        "fixture-model", 1, tmp_path / "unused-report.json", True))
    if auth_kind == "tokens":
        auth = TokenSessions(TOKENS)
    else:
        authority = ProjectApproverAllowlist({PROJECT: {REVIEWER.principal_id}})
        lab.approvals = SeparatedApprovalService(lab.approval_store, approvers=authority)
        auth = IntrospectionSessions(
            IntrospectionConfig("https://id.test", "https://id.test/check", "api", "client", "secret"),
            {"owner": ProjectMember(OPERATOR, PROJECT)},
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
                "active": True, "iss": "https://id.test", "aud": "api", "scope": "ghostlab",
                "token_type": "Bearer", "sub": "owner", "exp": 2_000_000_000})))
        if auth_kind == "wrapped-introspection":
            auth = Wrapped(auth)
    with TestClient(create_app(config(lab, auth)), **LOOPBACK) as client:
        # A synthetic label/retention decision cannot grant a live content capability.
        objective = "synthetic: Ramesh Example, +91 9876543210, Aadhaar 1234 5678 9012"
        spec = {**spec_json(), "objective": objective}
        response = client.post("/v1/campaigns", json=spec, headers={
            "Authorization": "Bearer operator", "Idempotency-Key": "private-data"})
        assert response.status_code == 403, response.text
        assert response.json()["reason"] == "live_content_not_qualified"
        assert not wire.calls
        assert client.get("/v1/session", headers={"Authorization": "Bearer operator"}).status_code == 200


def test_retention_refusal_on_paused_campaign_is_explained_and_does_not_spend_approval(tmp_path):
    lab = build_lab(tmp_path, Script(proposal()))
    with TestClient(create_app(config(lab, TokenSessions(TOKENS))), **LOOPBACK) as client:
        api = Api(client, lab)
        paused = api.paused()
        campaign = paused["record"]["campaign_id"]
    wire = CountingNetwork()
    lab.runtime = dataclasses.replace(lab.runtime, transport=wire)
    lab.live = LiveInferenceAuthorization("fixture-model", 1, tmp_path / "unused-report.json", True)
    with TestClient(create_app(config(lab, TokenSessions(TOKENS))), **LOOPBACK) as client:
        api = Api(client, lab)
        refused = api.post(f"/v1/campaigns/{campaign}/resume", key="resume")
        assert refused.status_code == 403 and refused.json()["reason"] == "live_content_not_qualified"
        result = api.decide(paused)
        assert result.status_code == 200, result.text
        assert result.json()["resumed"] is False
        assert result.json()["resume_error"]["reason"] == "live_content_not_qualified"
        assert lab.approval_store.get(PROJECT, paused["approval"]["approval_id"]).status == "approved"
        assert not wire.calls
        assert api.get(f"/v1/campaigns/{campaign}").status_code == 200
        cancelled = api.post(f"/v1/campaigns/{campaign}/cancel", key="cancel")
        assert cancelled.status_code in (200, 202), cancelled.text
        assert cancelled.json()["record"]["status"] == "cancelled"


def test_wrapping_introspection_does_not_bypass_separate_review_requirement(tmp_path):
    lab = build_lab(tmp_path, Script())
    auth = IntrospectionSessions(
        IntrospectionConfig("https://id.test", "https://id.test/check", "api", "client", "secret"),
        {"owner": ProjectMember(OPERATOR, PROJECT)},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "active": True, "iss": "https://id.test", "aud": "api", "scope": "ghostlab",
            "token_type": "Bearer", "sub": "owner", "exp": 2_000_000_000})))
    with TestClient(create_app(config(lab, Wrapped(auth))), **LOOPBACK) as client:
        response = client.get("/v1/session", headers={"Authorization": "Bearer token"})
        assert response.status_code == 403
    assert auth._closed


def test_missing_stored_review_decision_has_domain_error_not_attribute_error(tmp_path):
    lab = build_lab(tmp_path, Script(proposal()))
    with TestClient(create_app(config(lab, TokenSessions(TOKENS))), **LOOPBACK) as client:
        api = Api(client, lab)
        paused = api.paused()
        approval_id = paused["approval"]["approval_id"]
        # Persist an approved row, then simulate lost/corrupt decision evidence.
        lab.approve(client.app.state.plane._controller(
            TokenSessions(TOKENS).authenticate(httpx.Headers({"authorization": "Bearer operator"})).trusted
        ).state(paused["record"]["campaign_id"]))
        with db.write(lab.approval_store.path) as conn:
            conn.execute("UPDATE approvals SET decision_body = NULL WHERE project_id = ? AND approval_id = ?",
                         (PROJECT, approval_id))
        strict = SeparatedApprovalService(lab.approval_store, approvers=lab.approvals._approvers)
        with pytest.raises(ApprovalDenied) as error:
            strict.consume(project_id=PROJECT, approval_id=approval_id,
                           action={}, now=lab.clock())
        assert error.value.code == "not_approved"


def test_reviewer_can_discover_only_own_project_pending_approvals(stack):
    api = stack(proposal(), proposal())
    first = api.paused(key="first")
    second = api.paused(key="second")
    response = api.get("/v1/approvals?limit=1", who="reviewer")
    assert response.status_code == 200
    page = response.json()
    assert len(page) == 1
    after = page[0]["request"]["approval_id"]
    rest = api.get("/v1/approvals", who="reviewer", params={"after": after, "limit": 1}).json()
    assert len(rest) == 1
    expected = {first["approval"]["approval_id"], second["approval"]["approval_id"]}
    assert {after, rest[0]["request"]["approval_id"]} == expected
    assert api.get("/v1/approvals", who="outsider").json() == []
    assert api.get("/v1/approvals", who="operator").status_code == 403
    assert api.get("/v1/approvals?limit=101", who="reviewer").status_code == 422


def test_authentication_provider_failures_are_json_and_client_is_closed(tmp_path):
    lab = build_lab(tmp_path, Script())
    authority = ProjectApproverAllowlist({PROJECT: {REVIEWER.principal_id}})
    lab.approvals = SeparatedApprovalService(lab.approval_store, approvers=authority)
    auth = IntrospectionSessions(
        IntrospectionConfig("https://id.test", "https://id.test/check", "api", "client", "secret"), {},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"[" * 2000 + b"0" + b"]" * 2000)))
    with TestClient(create_app(config(lab, auth)), **LOOPBACK) as client:
        response = client.get("/v1/session", headers={"Authorization": "Bearer token"})
        assert response.status_code == 503
        assert response.json()["reason"] == "authentication_response"
        assert response.headers["retry-after"] == "1"
    assert auth._closed
