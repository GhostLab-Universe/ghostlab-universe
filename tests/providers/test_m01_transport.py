"""Retry, timeout, cancellation, redaction and configuration (FIXTURE transport only)."""

import pickle
import threading

import httpx
import pytest

from m01_fixtures import (PROPOSAL, SECRET, FakeTime, adapter, bundle, config, meta_response,
                          meta_text, request)
from ghostlab.contracts import BY_NAME
from ghostlab.providers import (Credential, ProviderConfig, ProviderConfigError,
                                ProviderRequestError, RetryPolicy, adapter_for, redact)

OK = (200, meta_response([meta_text(PROPOSAL)]))


def run(*responses, cfg=None, cancel=None, fake=None, **changes):
    meta, recorder, fake = adapter("meta_responses", *responses, cfg=cfg, fake=fake)
    reply = meta.complete(request(**changes), bundle(), cancel=cancel)
    return reply, recorder, fake, meta


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_statuses_retry_then_succeed(status):
    reply, recorder, fake, meta = run((status, {"error": {"message": "busy"}}), OK)
    assert reply.status == "completed" and len(recorder.requests) == 2
    assert [a.status_code for a in meta.last_attempts] == [status, 200]
    assert sum(fake.sleeps) == pytest.approx(0.5)  # base backoff, jitter fixed at 0


def test_retries_are_bounded_and_exponential():
    busy = (503, {"error": {"message": "overloaded", "code": "service_overloaded"}})
    reply, recorder, fake, _ = run(busy, busy, busy)
    assert len(recorder.requests) == 3  # max_attempts, never more
    assert reply.status == "failed" and reply.error.code == "provider_transient"
    assert reply.error.retryable is True and reply.error.side_effect == "none"
    assert sum(fake.sleeps) == pytest.approx(0.5 + 1.0)


def test_retry_after_header_is_honored_and_capped():
    limited = (429, {"error": {"message": "slow down"}}, {"retry-after": "2"})
    reply, _, fake, _ = run(limited, OK)
    assert reply.status == "completed" and sum(fake.sleeps) == pytest.approx(2.0)
    huge = (429, {"error": {"message": "slow down"}}, {"retry-after": "100000"})
    reply, recorder, fake, _ = run(huge, OK, timeout_ms=600_000,
                                   cfg=config(retry=RetryPolicy(retry_after_max_s=5.0)))
    assert len(recorder.requests) == 2 and sum(fake.sleeps) == pytest.approx(5.0)


@pytest.mark.parametrize("status,code", [(400, "provider_permanent"), (401, "unauthenticated"),
                                         (403, "forbidden"), (404, "provider_permanent"),
                                         (413, "provider_permanent"), (504, "deadline_exceeded")])
def test_non_retryable_statuses_fail_once(status, code):
    reply, recorder, _, _ = run((status, {"error": {"message": "no", "type": "x"}}))
    assert len(recorder.requests) == 1
    assert reply.status == "failed" and reply.error.code == code and not reply.error.retryable


def test_client_timeout_is_a_deadline_not_retried():
    reply, recorder, _, _ = run(httpx.ReadTimeout("slow"))
    assert len(recorder.requests) == 1 and reply.error.code == "deadline_exceeded"


def test_connection_errors_retry():
    reply, recorder, _, _ = run(httpx.ConnectError("refused"), OK)
    assert reply.status == "completed" and len(recorder.requests) == 2


def test_backoff_that_would_pass_the_deadline_stops_early():
    reply, recorder, fake, _ = run((503, {}), OK, timeout_ms=300)
    assert len(recorder.requests) == 1 and fake.sleeps == []
    assert reply.status == "failed" and reply.error.code == "provider_transient"


def test_cancelled_before_send_makes_no_request():
    cancel = threading.Event()
    cancel.set()
    reply, recorder, _, _ = run(cancel=cancel)
    assert recorder.requests == [] and reply.error.code == "cancelled"


def test_cancellation_during_backoff_stops_retrying():
    cancel = threading.Event()
    fake = FakeTime()
    original = fake.sleep

    def sleep_then_cancel(seconds):
        original(seconds)
        cancel.set()

    fake.sleep = sleep_then_cancel
    reply, recorder, _, _ = run((503, {}), OK, cancel=cancel, fake=fake)
    assert len(recorder.requests) == 1 and reply.error.code == "cancelled"


def test_secrets_never_appear_in_errors_reprs_or_pickles():
    echo = (401, {"error": {"message": f"key {SECRET} rejected; Bearer {SECRET}"}})
    reply, _, _, meta = run(echo)
    assert SECRET not in reply.model_dump_json()
    assert SECRET not in repr(meta.config) and SECRET not in str(meta.config.credential)
    with pytest.raises(TypeError):
        pickle.dumps(meta.config.credential)
    assert redact("Authorization: Bearer abcdefghijkl123") == "Authorization: Bearer ***"


def test_config_requires_explicit_model_https_and_env_key(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("GHOSTLAB_META_MODEL_ID", raising=False)
    with pytest.raises(ProviderConfigError, match="GHOSTLAB_META_MODEL_ID"):
        ProviderConfig.from_env("meta_responses")
    monkeypatch.setenv("GHOSTLAB_META_MODEL_ID", "fixture-model")
    with pytest.raises(ProviderConfigError, match="MODEL_API_KEY"):
        ProviderConfig.from_env("meta_responses")
    monkeypatch.setenv("MODEL_API_KEY", SECRET)
    loaded = ProviderConfig.from_env("meta_responses")
    assert loaded.base_url == "https://api.meta.ai/v1" and SECRET not in repr(loaded)
    with pytest.raises(ProviderConfigError):
        config(base_url="http://api.meta.ai/v1")
    with pytest.raises(ProviderConfigError):
        config(model="")
    with pytest.raises(ProviderConfigError):
        Credential("  ", "test")


def test_no_fallback_between_providers():
    meta, recorder, _ = adapter("meta_responses")
    with pytest.raises(ProviderConfigError):
        meta.complete(request("baseten_chat_completions"), bundle())
    with pytest.raises(ProviderConfigError):
        meta.complete(request(model="muse-spark-1.3"), bundle())
    assert recorder.requests == []
    assert type(adapter_for(config("baseten_chat_completions"))).__name__ == "BasetenChatAdapter"


def test_mismatched_context_bundle_and_unknown_schema_rejected():
    meta, recorder, _ = adapter("meta_responses")
    with pytest.raises(ProviderRequestError):
        meta.complete(request(), bundle(bundle_id="bundle-other"))
    with pytest.raises(ProviderRequestError):
        meta.complete(request(output_schema={"name": "experiment_proposal", "version": 2}),
                      bundle())
    assert recorder.requests == [] and "experiment_proposal" in BY_NAME


def test_retry_policy_bounds():
    with pytest.raises(ProviderConfigError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ProviderConfigError):
        RetryPolicy(max_attempts=6)
