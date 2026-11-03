import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, Request
from ghostlab.api.oauth import OAuthSessions, auth_router, COOKIE
from ghostlab.api.middleware import RequestGuard
from ghostlab.security.redaction import Redactor

ORIGIN = "http://localhost:5199"

def stack(provider="github", *, subject=None, status=200, clock=None):
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "POST":
            body = {"access_token": "test-provider-token", "token_type": "bearer"}
        else:
            body = {"id": 123} if provider == "github" else {"sub": "google-123"}
            if subject is not None: body = subject
        return httpx.Response(status, stream=httpx.ByteStream(json.dumps(body).encode()))
    sessions = OAuthSessions(ORIGIN, {provider: ("client", "secret")}, {
        "github:123": {"principal_id": "alice", "project_id": "project-a"},
        "google:google-123": {"principal_id": "bob", "project_id": "project-b"},
    }, transport=httpx.MockTransport(handler), **({"clock": clock} if clock else {}))
    app = FastAPI(); app.include_router(auth_router(sessions))
    @app.get("/v1/private")
    async def private(request: Request):
        s = request.state.session
        return {"principal": s.principal.principal_id, "project": s.project_id}
    @app.post("/v1/write")
    async def write(): return {"ok": True}
    app.add_middleware(RequestGuard, sessions=sessions, redactor=Redactor())
    return app, sessions, calls

async def begin(client, provider="github"):
    response = await client.get(f"/v1/auth/{provider}/start")
    assert response.status_code == 302
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert "HttpOnly" in response.headers["set-cookie"]
    return params["state"][0]

@pytest.mark.parametrize("provider,principal,project", [("github", "alice", "project-a"), ("google", "bob", "project-b")])
def test_login_membership_csrf_logout(provider, principal, project):
    async def run():
        app, sessions, calls = stack(provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
            assert (await client.get("/v1/private")).status_code == 401
            state = await begin(client, provider)
            response = await client.get(f"/v1/auth/{provider}/callback", params={"state": state, "code": "test-code"})
            assert response.status_code == 303
            assert len(calls) == 2
            assert "code_verifier=" in calls[0].content.decode()
            assert "test-provider-token" not in response.text + str(response.headers)
            assert (await client.get("/v1/private")).json() == {"principal": principal, "project": project}
            assert (await client.post("/v1/write")).status_code == 403
            assert (await client.post("/v1/write", headers={"Origin": "http://localhost:9999"})).status_code == 403
            assert (await client.post("/v1/write", headers={"Origin": ORIGIN})).status_code == 200
            assert (await client.get(f"/v1/auth/{provider}/callback", params={"state": state, "code": "test-code"})).status_code == 403
            assert (await client.post("/v1/auth/logout", headers={"Origin": ORIGIN, "X-GhostLab-Intent": "logout"})).status_code == 200
            assert not sessions.sessions
            assert (await client.get("/v1/private")).status_code == 401
    asyncio.run(run())

@pytest.mark.parametrize("case", ["state", "cookie", "provider", "expired", "unassigned", "redirect", "bad-id"])
def test_refusals_never_create_session(case):
    async def run():
        now = [100.0]
        app, sessions, calls = stack(subject={"id": 999} if case == "unassigned" else ({"id": True} if case == "bad-id" else None), status=302 if case == "redirect" else 200, clock=lambda: now[0])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
            state = await begin(client)
            if case == "state": state = "wrong"
            if case == "cookie": client.cookies.clear()
            if case == "expired": now[0] += 301
            provider = "google" if case == "provider" else "github"
            response = await client.get(f"/v1/auth/{provider}/callback", params={"state": state, "code": "test-code"})
            assert response.status_code in {403, 502}
            assert not sessions.sessions
            if case in {"state", "cookie", "provider", "expired"}: assert not calls
    asyncio.run(run())

def test_session_expires_and_origin_configuration_is_not_arbitrary():
    with pytest.raises(ValueError): OAuthSessions("http://evil.example", {}, {})
    async def run():
        now = [1.0]; app, sessions, _ = stack(clock=lambda: now[0])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
            state = await begin(client)
            await client.get("/v1/auth/github/callback", params={"state": state, "code": "test-code"})
            assert (await client.get("/v1/private")).status_code == 200
            now[0] += 28801
            assert (await client.get("/v1/private")).status_code == 401
    asyncio.run(run())

def test_only_explicit_login_paths_are_public_and_no_identity_is_advertised():
    from ghostlab.api.app import create_app
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app()), base_url=ORIGIN) as client:
            assert (await client.get("/v1/auth/status")).json() == {"enabled": False, "providers": [], "signed_in": False}
            assert (await client.get("/v1/auth/github/start")).status_code == 503
            assert (await client.get("/v1/auth/google/start")).status_code == 503
            for path in ("/v1/auth/other/start", "/v1/auth/status/extra", "/v1/session", "/v1/sources/github/inspect"):
                assert (await client.get(path)).status_code == 401
            assert (await client.post("/v1/auth/status")).status_code == 401
            assert (await client.post("/v1/auth/logout")).status_code == 401
            response = await client.get("/v1/auth/status", headers={"Origin": "https://evil.example"})
            assert response.status_code == 403
    asyncio.run(run())
