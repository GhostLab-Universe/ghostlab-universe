"""External identity fixture through real middleware, SQLite and approval policy."""

import dataclasses
import warnings

import httpx
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from ghostlab.api import create_app
from ghostlab.api.introspection import (IntrospectionConfig, IntrospectionSessions,
                                         ProjectMember, configure_introspection)
from ghostlab.api.plane import ApiConfig
from ghostlab.policy import ProjectApproverAllowlist
from ghostlab.orchestration.inference import LiveInferenceAuthorization

from conftest import LOOPBACK, build_lab, spec_json
from m05_harness import OPERATOR, PROJECT, REVIEWER, STRANGER, Script, proposal


def test_verified_tokens_reach_real_api_and_self_approval_is_refused(tmp_path):
    lab = build_lab(tmp_path, Script(proposal()))
    authority = ProjectApproverAllowlist({PROJECT: {OPERATOR.principal_id, REVIEWER.principal_id}})
    config = ApiConfig(services=lab.services(), sessions=None, approvers=authority,
                       graph_db=lab.paths.graph_db, app_db=lab.paths.app_db)
    provider = IntrospectionConfig("https://id.example/realm", "https://id.example/check",
                                    "ghostlab", "client", "fixture-secret")
    revoked = set()

    def introspect(request):
        from urllib.parse import parse_qs
        token = parse_qs(request.content.decode())["token"][0]
        return httpx.Response(200, json={"active": token not in revoked, "iss": provider.issuer,
            "token_type": "Bearer", "aud": "ghostlab", "scope": "ghostlab", "sub": token, "exp": 2_000_000_000})

    sessions = IntrospectionSessions(provider, {
        "owner-token": ProjectMember(OPERATOR, PROJECT),
        "reviewer-token": ProjectMember(REVIEWER, PROJECT),
        "outsider-token": ProjectMember(STRANGER, "project-b")},
        transport=httpx.MockTransport(introspect))
    secure = configure_introspection(config, sessions, approvers=authority)
    try:
        with TestClient(create_app(secure), **LOOPBACK) as client:
            auth = {"Authorization": "Bearer owner-token"}
            assert client.get("/v1/session").status_code == 401
            session = client.get("/v1/session", headers=auth).json()
            assert session["auth_mode"] == "oidc_introspection"
            # An authenticated token does not certify the entire deployment.
            assert session["production_authenticated"] is False
            response = client.post("/v1/campaigns", headers={**auth, "Idempotency-Key": "start"},
                                   json=spec_json())
            assert response.status_code in (200, 201, 202)
            campaign = response.json()["record"]["campaign_id"]
            # Wait for the real worker to reach its approval pause.
            import time
            for _ in range(200):
                view = client.get(f"/v1/campaigns/{campaign}", headers=auth).json()
                if view.get("approval"):
                    break
                time.sleep(0.025)
            assert view.get("approval")
            approval = view["approval"]
            body = {"schema_version": 1, "approval_id": approval["approval_id"],
                    "action_digest": approval["action_digest"], "decision": "approve", "reason": None}
            path = f"/v1/approvals/{approval['approval_id']}/decision"
            refused = client.post(path, headers={**auth, "Idempotency-Key": "self"}, json=body)
            assert refused.status_code == 403
            approved = client.post(path, headers={"Authorization": "Bearer reviewer-token",
                                   "Idempotency-Key": "review"}, json=body)
            assert approved.status_code == 200
            assert client.get(f"/v1/campaigns/{campaign}", headers={
                "Authorization": "Bearer outsider-token"}).status_code in (403, 404)
            revoked.add("owner-token")
            assert client.get("/v1/session", headers=auth).status_code == 401
    finally:
        sessions.close()


def test_external_auth_cannot_be_installed_with_legacy_self_approval_policy(tmp_path):
    lab = build_lab(tmp_path, Script())
    sessions = IntrospectionSessions(
        IntrospectionConfig("https://id.example", "https://id.example/check", "api", "id", "secret"),
        {}, transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    try:
        config = ApiConfig(services=lab.services(), sessions=sessions,
            approvers=lab.approvals._approvers, graph_db=lab.paths.graph_db, app_db=lab.paths.app_db)
        with pytest.raises(ValueError, match="independent approval"):
            create_app(config)
        # Existing perimeter still rejects hosting this unqualified deployment.
        with pytest.raises(Exception, match="hosted"):
            create_app(dataclasses.replace(config, environ={"GHOSTLAB_DEPLOYMENT": "hosted"}))
    finally:
        sessions.close()
