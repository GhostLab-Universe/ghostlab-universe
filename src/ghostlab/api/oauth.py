"""Optional GitHub/Google login for the single-process, loopback application.

This authenticates provider identities; it does not enable hosted deployment.
Membership is an operator-owned provider:subject -> principal/project mapping.
No email-based linking, self-enrollment or repository scope is requested.
"""
import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from ghostlab.contracts import Principal
from ghostlab.contracts.primitives import Identifier
from pydantic import TypeAdapter
from .sessions import ApiSession
from .account_models import AuthStatus, LogoutResult, ACCOUNT_ERRORS, account_error

PROVIDERS = {
    "github": ("https://github.com/login/oauth/authorize", "https://github.com/login/oauth/access_token", "https://api.github.com/user", ""),
    "google": ("https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token", "https://openidconnect.googleapis.com/v1/userinfo", "openid profile email"),
}
COOKIE = "ghostlab_session"
STATE_COOKIE = "ghostlab_login"

def cookie(headers, name):
    try:
        values = SimpleCookie(); values.load(headers.get("cookie", ""))
        return values[name].value if name in values else ""
    except CookieError:
        return ""

class OAuthSessions:
    def __init__(self, origin, credentials, members, *, transport=None, clock=time.monotonic):
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("OAuth origin must be an exact loopback browser origin, without a path")
        if not credentials or not members:
            raise ValueError("OAuth requires provider credentials and explicit member assignments")
        if any(name not in PROVIDERS or len(pair) != 2 or not all(isinstance(x, str) and x.strip() for x in pair) for name, pair in credentials.items()):
            raise ValueError("Invalid OAuth provider configuration")
        self.origin, self.credentials = origin, credentials
        self.members = {}
        for identity, member in members.items():
            provider, separator, subject = identity.partition(":")
            if provider not in PROVIDERS or not separator or not subject or len(subject) > 200:
                raise ValueError("Members must use provider:immutable-subject identifiers")
            principal = Principal(principal_id=member["principal_id"], kind="human")
            project = TypeAdapter(Identifier).validate_python(member["project_id"])
            self.members[identity] = ApiSession(principal, project, "configured_tokens")
        self.transport, self.clock = transport, clock
        self.pending, self.sessions = {}, {}
        self.slots = asyncio.Semaphore(4)

    @classmethod
    def from_environment(cls):
        credentials = {}
        for provider in PROVIDERS:
            prefix = f"GHOSTLAB_{provider.upper()}_"
            client, secret = os.environ.get(prefix + "CLIENT_ID"), os.environ.get(prefix + "CLIENT_SECRET")
            if client or secret:
                if not client or not secret:
                    raise ValueError(f"Both {provider} client ID and secret are required")
                credentials[provider] = (client, secret)
        return cls(os.environ.get("GHOSTLAB_AUTH_ORIGIN", "http://localhost:5199"), credentials,
                   json.loads(os.environ.get("GHOSTLAB_AUTH_MEMBERS", "{}")))

    def prune(self):
        now = self.clock()
        self.pending = {key: value for key, value in self.pending.items() if value[0] > now}
        self.sessions = {key: value for key, value in self.sessions.items() if value[0] > now}

    def authenticate(self, headers):
        token = cookie(headers, COOKIE)
        if not token or len(token) > 128:
            return None
        value = self.sessions.get(hashlib.sha256(token.encode()).hexdigest())
        return value[1] if value and value[0] > self.clock() else None

    def revoke(self, headers):
        token = cookie(headers, COOKIE)
        self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    async def fetch_json(self, client, method, url, **kwargs):
        async with client.stream(method, url, **kwargs) as response:
            if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("Provider refused sign-in")
            data = bytearray()
            async for chunk in response.aiter_raw():
                if len(data) + len(chunk) > 65536:
                    raise ValueError("Provider response too large")
                data.extend(chunk)
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError("Invalid provider response")
            return value

    async def exchange(self, provider, code, verifier):
        client_id, client_secret = self.credentials[provider]
        _, endpoint, userinfo, _ = PROVIDERS[provider]
        async with asyncio.timeout(12):
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False, timeout=5,
                                         headers={"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "GhostLab-login"}) as client:
                token = await self.fetch_json(client, "POST", endpoint, data={"client_id": client_id, "client_secret": client_secret,
                    "code": code, "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": f"{self.origin}/v1/auth/{provider}/callback"})
                access = token.get("access_token")
                if not isinstance(access, str) or not 1 <= len(access) <= 8192 or token.get("token_type", "").lower() != "bearer":
                    raise ValueError("Invalid provider token")
                profile = await self.fetch_json(client, "GET", userinfo, headers={"Authorization": f"Bearer {access}"})
                subject = profile.get("id" if provider == "github" else "sub")
                if provider == "github":
                    if type(subject) is not int or subject <= 0: raise ValueError("Invalid GitHub identity")
                elif not isinstance(subject, str) or not subject or len(subject) > 200:
                    raise ValueError("Invalid Google identity")
                return self.members.get(f"{provider}:{subject}")

def auth_router(sessions):
    router = APIRouter(prefix="/v1/auth", responses=ACCOUNT_ERRORS)
    oauth = sessions if isinstance(sessions, OAuthSessions) else None

    @router.get("/status", response_model=AuthStatus, operation_id="getAuthStatus")
    async def status(request: Request):
        return {"enabled": oauth is not None, "providers": list(oauth.credentials) if oauth else [],
                "signed_in": bool(oauth and oauth.authenticate(request.headers))}

    @router.get("/{provider}/start", response_class=RedirectResponse, status_code=302, operation_id="startSignIn")
    async def start(provider: str):
        if oauth is None or provider not in oauth.credentials:
            return account_error("This sign-in provider is not configured.", 503)
        oauth.prune()
        if len(oauth.pending) >= 128:
            return account_error("Sign-in is busy. Try again shortly.", 429)
        state, binding, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        oauth.pending[state] = (oauth.clock() + 300, provider, binding, verifier)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": oauth.credentials[provider][0], "redirect_uri": f"{oauth.origin}/v1/auth/{provider}/callback",
                  "response_type": "code", "scope": PROVIDERS[provider][3], "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        response = RedirectResponse(PROVIDERS[provider][0] + "?" + urlencode(params), status_code=302)
        response.set_cookie(STATE_COOKIE, binding, max_age=300, httponly=True, samesite="lax", secure=oauth.origin.startswith("https:"), path="/v1/auth")
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.get("/{provider}/callback", response_class=RedirectResponse, status_code=303, operation_id="completeSignIn")
    async def callback(provider: str, request: Request):
        denied = lambda status=403: account_error("Sign-in could not be completed. Retry from Account & sign-in, or ask the owner to check your access.", status)
        if oauth is None or provider not in oauth.credentials:
            return denied()
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        record = oauth.pending.get(state)
        if not record or record[0] <= oauth.clock() or record[1] != provider or not secrets.compare_digest(record[2], cookie(request.headers, STATE_COOKIE)):
            return denied()
        oauth.pending.pop(state, None)
        if not 1 <= len(code) <= 2048 or oauth.slots.locked():
            return denied(429 if oauth.slots.locked() else 403)
        try:
            async with oauth.slots:
                session = await oauth.exchange(provider, code, record[3])
        except (ValueError, TypeError, AttributeError, RecursionError, httpx.HTTPError, TimeoutError):
            return denied(502)
        if session is None:
            return denied()
        oauth.prune()
        if len(oauth.sessions) >= 256: return denied(429)
        oauth.revoke(request.headers)
        token = secrets.token_urlsafe(32)
        oauth.sessions[hashlib.sha256(token.encode()).hexdigest()] = (oauth.clock() + 28800, session)
        response = RedirectResponse(oauth.origin + "/#/", status_code=303)
        response.set_cookie(COOKIE, token, max_age=28800, httponly=True, samesite="lax", secure=oauth.origin.startswith("https:"), path="/")
        response.delete_cookie(STATE_COOKIE, path="/v1/auth")
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.post("/logout", response_model=LogoutResult, operation_id="signOut")
    async def logout(request: Request):
        if oauth is None or request.headers.get("origin") != oauth.origin or request.headers.get("x-ghostlab-intent") != "logout":
            return account_error("Sign-out request refused.", 403)
        oauth.revoke(request.headers)
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(COOKIE, path="/")
        return response
    return router
