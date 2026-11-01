import json

import httpx
import pytest
from starlette.datastructures import Headers

from ghostlab.api.introspection import (IntrospectionConfig, IntrospectionSessions,
                                         ProjectMember)
from ghostlab.contracts import Principal
from ghostlab.api.errors import ApiError

NOW = 1_800_000_000
CONFIG = IntrospectionConfig("https://identity.example/realm", "https://identity.example/check",
                             "ghostlab-api", "ghostlab-client", "fixture-client-secret")
OWNER = ProjectMember(Principal(principal_id="owner", kind="human"), "project-a")
REVIEWER = ProjectMember(Principal(principal_id="reviewer", kind="human"), "project-a")
CLAIMS = {"active": True, "iss": CONFIG.issuer, "aud": CONFIG.audience, "sub": "user-1",
          "scope": "ghostlab profile", "exp": NOW + 60, "token_type": "Bearer"}


def adapter(body=None, status=200):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=CLAIMS if body is None else body)

    return IntrospectionSessions(CONFIG, {"user-1": OWNER, "user-2": REVIEWER},
                                  transport=httpx.MockTransport(respond), clock=lambda: NOW), requests


def headers(token="fixture-access-token"):
    return Headers({"authorization": "Bearer " + token})


def test_external_identity_is_mapped_by_server_not_claimed_project():
    auth, calls = adapter({**CLAIMS, "project_id": "project-b", "principal_id": "admin"})
    try:
        session = auth.authenticate(headers())
        assert session.project_id == "project-a" and session.principal.principal_id == "owner"
        assert session.auth_mode == "oidc_introspection"
        assert calls[0].url == CONFIG.endpoint and b"token=" in calls[0].content
    finally:
        auth.close()


@pytest.mark.parametrize("change", [
    {"active": False}, {"active": 1}, {"exp": NOW}, {"exp": True}, {"exp": "soon"},
    {"iss": "https://attacker.example"}, {"aud": "another-api"}, {"aud": [1]},
    {"sub": "unknown-user"}, {"sub": []}, {"scope": "profile"}, {"scope": ["ghostlab"]},
    {"nbf": NOW + 1}, {"iat": NOW + 1}, {"token_type": []}, {"token_type": "basic"},
])
def test_invalid_claims_fail_closed(change):
    auth, _ = adapter({**CLAIMS, **change})
    try:
        assert auth.authenticate(headers()) is None
    finally:
        auth.close()


@pytest.mark.parametrize("value", ["", "Basic abc", "Bearer x y", "Bearer " + "x" * 8193])
def test_invalid_credentials_do_not_contact_the_provider(value):
    auth, requests = adapter()
    try:
        assert auth.authenticate(Headers({"authorization": value})) is None
        assert not requests
    finally:
        auth.close()


def test_duplicate_headers_are_refused():
    auth, calls = adapter()
    try:
        assert auth.authenticate(Headers(raw=[(b"authorization", b"Bearer one"),
                                               (b"authorization", b"Bearer two")])) is None
        assert not calls
    finally:
        auth.close()


def test_revocation_is_not_hidden_by_a_cache():
    active = [True]
    auth = IntrospectionSessions(CONFIG, {"user-1": OWNER}, clock=lambda: NOW,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={**CLAIMS, "active": active[0]})))
    try:
        assert auth.authenticate(headers()) is not None
        active[0] = False
        assert auth.authenticate(headers()) is None
    finally:
        auth.close()


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_error_responses_fail_closed_without_retry(status):
    auth, calls = adapter(status=status)
    try:
        with pytest.raises(ApiError) as error:
            auth.authenticate(headers())
        assert error.value.status == 503 and len(calls) == 1
    finally:
        auth.close()


def test_oversized_response_is_refused():
    auth, _ = adapter({**CLAIMS, "extra": "x" * 70_000})
    try:
        with pytest.raises(ApiError) as error:
            auth.authenticate(headers())
        assert error.value.status == 503
    finally:
        auth.close()


def test_configuration_does_not_disclose_client_secret():
    assert "fixture-client-secret" not in repr(CONFIG)
    with pytest.raises(ValueError):
        IntrospectionSessions.from_environment({}, environ={})
    with pytest.raises(ValueError):
        IntrospectionConfig("http://identity.example", CONFIG.endpoint, "api", "id", "secret")


def test_provider_timeout_does_not_authenticate_or_retry():
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("fixture failure", request=request)

    auth = IntrospectionSessions(CONFIG, {"user-1": OWNER}, transport=httpx.MockTransport(timeout))
    try:
        with pytest.raises(ApiError) as error:
            auth.authenticate(headers())
        assert error.value.status == 503 and len(calls) == 1
    finally:
        auth.close()
