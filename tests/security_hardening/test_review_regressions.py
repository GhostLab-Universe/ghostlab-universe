"""Independent hostile-response probes: no real identity or inference endpoint."""
import asyncio
import gzip
import json
import time

import httpx
import pytest

from ghostlab.api.errors import ApiError, describe
from ghostlab.api.introspection import IntrospectionConfig, IntrospectionSessions
from ghostlab.security.redaction import Redactor
from ghostlab.security.retention import RetentionRefused, RetentionDecision
from test_introspection import CONFIG, CLAIMS, NOW, OWNER, headers


class Bytes(httpx.AsyncByteStream):
    def __init__(self, data, *, delay=0):
        self.data, self.delay, self.reads, self.closed = data, delay, 0, False

    async def __aiter__(self):
        self.reads += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        yield self.data

    async def aclose(self):
        self.closed = True


def sessions(handler):
    return IntrospectionSessions(CONFIG, {"user-1": OWNER}, clock=lambda: NOW,
                                 transport=httpx.MockTransport(handler))


def test_compression_bomb_is_rejected_without_reading_or_decoding():
    stream = Bytes(gzip.compress(b"x" * (64 * 1024 * 1024)))
    async def reply(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
    auth = sessions(reply)
    with pytest.raises(ApiError) as error:
        auth.authenticate(headers())
    assert error.value.status == 503
    assert stream.reads == 0 and stream.closed


@pytest.mark.parametrize("where", ["headers", "body"])
def test_wall_clock_deadline_cancels_the_actual_operation(where):
    cancelled = []
    stream = Bytes(json.dumps(CLAIMS).encode(), delay=2 if where == "body" else 0)
    async def reply(request):
        if where == "headers":
            try:
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return httpx.Response(200, stream=stream)
    auth = sessions(reply)
    auth._deadline_seconds = .05
    started = time.monotonic()
    with pytest.raises(ApiError) as error:
        auth.authenticate(headers())
    assert error.value.status == 503 and time.monotonic() - started < .8
    assert cancelled if where == "headers" else stream.closed
    # Cancellation did not leak a semaphore slot.
    assert auth._slots.acquire(blocking=False)
    auth._slots.release()


def test_capacity_refusal_is_retryable_and_not_a_bad_token():
    async def run():
        requests = []
        async def reply(request):
            requests.append(request)
            await asyncio.sleep(.03)
            return httpx.Response(200, json=CLAIMS)
        auth = sessions(reply)
        results = await asyncio.gather(*(auth.authenticate_async(headers(str(i))) for i in range(12)),
                                       return_exceptions=True)
        assert sum(not isinstance(x, Exception) for x in results) == 8
        denied = [x for x in results if isinstance(x, Exception)]
        assert len(denied) == 4
        assert all(isinstance(x, ApiError) and x.status == 503 and x.retryable
                   and x.headers.get("Retry-After") == "1" for x in denied)
        assert len(requests) == 8
    asyncio.run(run())


def test_redirect_to_another_host_is_never_followed():
    calls = []
    async def reply(request):
        calls.append(request)
        if request.url.host == "identity.example":
            return httpx.Response(307, headers={"location": "https://attacker.example/check"})
        return httpx.Response(200, json=CLAIMS)
    auth = sessions(reply)
    with pytest.raises(ApiError):
        auth.authenticate(headers())
    assert len(calls) == 1 and calls[0].url.host == "identity.example"


@pytest.mark.parametrize("audience", ["other-api", ["other-api"], [CONFIG.audience, 1], None, {}])
def test_wrong_audiences_cannot_create_a_session(audience):
    auth = sessions(lambda request: httpx.Response(200, json={**CLAIMS, "aud": audience}))
    assert auth.authenticate(headers()) is None


def test_missing_token_type_is_not_a_bearer_token():
    claims = {k: v for k, v in CLAIMS.items() if k != "token_type"}
    auth = sessions(lambda request: httpx.Response(200, json=claims))
    assert auth.authenticate(headers()) is None


def test_deep_json_returns_typed_service_failure():
    auth = sessions(lambda request: httpx.Response(200, content=b"[" * 2000 + b"0" + b"]" * 2000))
    with pytest.raises(ApiError) as error:
        auth.authenticate(headers())
    status, body, _ = describe(error.value, Redactor())
    assert status == 503 and body.reason == "authentication_response"


@pytest.mark.parametrize("url", ["https://identity.example/\ncheck", "https://identity.example/\tcheck",
                                   "https://identity.example/ check", "https://identity.example/\rcheck"])
def test_url_controls_are_rejected_at_configuration(url):
    with pytest.raises(ValueError):
        IntrospectionConfig(CONFIG.issuer, url, "audience", "client", "secret")


def test_oidc_client_secret_is_redacted_as_a_literal():
    redactor = Redactor.from_env(environ={"GHOSTLAB_OIDC_CLIENT_SECRET": "unusual-provider-secret"})
    assert "unusual-provider-secret" not in redactor.text("error unusual-provider-secret happened")


def test_retention_refusal_has_specific_forbidden_error():
    status, body, _ = describe(RetentionRefused("untrusted text"), Redactor())
    assert status == 403 and body.reason == "live_content_not_qualified"
    assert "untrusted text" not in body.error.message


def test_meta_continuation_choice_must_be_recorded():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    args = dict(provider="meta_responses", endpoint="https://api.meta.ai/v1", model="fixture",
                reviewed_at=now, reviewer_id="reviewer", provider_policy_url="https://example.test/policy",
                account_controls_verified=True)
    check = dict(provider=args["provider"], endpoint=args["endpoint"], model="fixture",
                 data_classification="synthetic", now=now)
    with pytest.raises(RetentionRefused, match="continuation"):
        RetentionDecision(**args).require(**check)
    decision = RetentionDecision(**args, continuation_policy="stateful-provider-storage-reviewed")
    decision.require(**check)  # metadata only; this never permits content transmission
