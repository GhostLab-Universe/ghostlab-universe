"""Regressions for review findings 1, 2, 3 and 6 (offline MOCK transport; not live inference)."""

import importlib.util
import json

import httpx
import pytest

from m01_fixtures import (MODEL, PROPOSAL, ROOT, SECRET, FakeTime, Recorder, adapter, bundle,
                          chat_call, chat_response, config, meta_call, meta_response, meta_text,
                          request)
from ghostlab.providers import ProviderRequestError, RetryPolicy, Runtime
from ghostlab.providers.http import MAX_ERROR_BODY_BYTES, MAX_RESPONSE_BYTES

spec = importlib.util.spec_from_file_location("probe_provider", ROOT / "scripts" / "probe_provider.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

LIVE = ["--provider", "meta_responses", "--live", "--model", "fixture-model",
        "--confirm-paid-calls"]
BUSY = (503, {"error": {"code": "server_error", "message": "busy"}})
BASETEN = "baseten_chat_completions"


def mock_runtime(*responses):
    recorder, fake = Recorder(*responses), FakeTime()
    return Runtime(transport=httpx.MockTransport(recorder), sleep=fake.sleep,
                   monotonic=fake.monotonic, jitter=lambda: 0.0), recorder


# --- finding 1: the paid-call cap counts every HTTP attempt, retries included ---------------

def test_probe_cap_counts_retries_as_attempts(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_API_KEY", SECRET)
    runtime, recorder = mock_runtime(BUSY, BUSY, (200, meta_response([meta_text("ready")])))
    path = tmp_path / "r.json"
    probe.main([*LIVE, "--max-calls", "1", "--report", str(path)], runtime=runtime)
    report = json.loads(path.read_text())
    assert len(recorder.requests) == 1  # the retry was not sent
    assert report["http_attempts"] == 1 and report["calls_made"] == 1
    auth = next(c for c in report["checks"] if c["check"] == "auth_minimal")
    assert auth["status"] == "failed" and "attempt cap" in auth["detail"]
    assert SECRET not in path.read_text()


def test_probe_reports_attempts_separately_from_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_API_KEY", SECRET)
    runtime, recorder = mock_runtime(BUSY, BUSY, (200, meta_response([meta_text("ready")])))
    path = tmp_path / "r.json"
    probe.main([*LIVE, "--max-calls", "3", "--report", str(path)], runtime=runtime)
    report = json.loads(path.read_text())
    assert len(recorder.requests) == 3 == report["http_attempts"]
    assert report["calls_made"] == 1
    statuses = {c["check"]: c["status"] for c in report["checks"]}
    assert statuses["auth_minimal"] == "passed"
    assert statuses["tool_roundtrip"] == statuses["structured_proposal"] == "skipped_call_cap"


def test_attempt_gate_is_consulted_before_every_attempt():
    meta, recorder, fake = adapter("meta_responses", BUSY, BUSY,
                                   (200, meta_response([meta_text("ok")])))
    allowed = iter([True, True, False])
    meta.runtime.attempt_gate = lambda: next(allowed)
    reply = meta.complete(request(), bundle())
    assert len(recorder.requests) == 2
    assert reply.status == "failed" and reply.error.code == "budget_exhausted"
    assert len(meta.last_attempts) == 2  # an attempt that was never sent is not recorded


# --- finding 2: truncated or filtered replies never expose executable tool calls ------------

@pytest.mark.parametrize("finish,status", [("length", "incomplete"),
                                           ("content_filter", "refused")])
def test_baseten_truncated_or_filtered_calls_are_withheld(finish, status):
    call = chat_call("call_cut", arguments=json.dumps(
        {"order_id": "synthetic-order-1", "customer_id": "synthetic-customer-1",
         "idempotency_key": "refund-intent-1"}))  # syntactically valid arguments
    baseten, recorder, _ = adapter(BASETEN, (200, chat_response(tool_calls=[call],
                                                                finish=finish)))
    reply = baseten.complete(request(BASETEN), bundle())
    assert reply.status == status and reply.tool_calls == []
    # The withheld call cannot be answered on the next turn: nothing is pending.
    follow_up = request(BASETEN, continuation=reply.continuation, tool_results=[
        {"provider_call_id": "call_cut", "output_json": "{}", "is_error": False}])
    with pytest.raises(ProviderRequestError):
        baseten.complete(follow_up, bundle())
    assert len(recorder.requests) == 1


def test_baseten_stop_with_calls_still_proposes_them():
    baseten, _, _ = adapter(BASETEN, (200, chat_response(tool_calls=[chat_call()],
                                                         finish="stop")))
    reply = baseten.complete(request(BASETEN), bundle())
    assert reply.status == "tool_calls" and len(reply.tool_calls) == 1


def test_meta_incomplete_reply_withholds_calls():
    meta, _, _ = adapter("meta_responses",
                         (200, meta_response([meta_call("call_cut")], status="incomplete")))
    reply = meta.complete(request(), bundle())
    assert reply.status == "incomplete" and reply.tool_calls == []


# --- finding 3: known credentials never appear in any serialized reply ----------------------

def _clean(reply):
    serialized = reply.model_dump_json()
    assert SECRET not in serialized and SECRET not in json.dumps(reply.model_dump())
    return serialized


def test_meta_http_200_failed_response_redacts_credential():
    body = meta_response([], status="failed")
    body["error"] = {"code": "invalid_request", "message": f"bad key {SECRET} supplied"}
    meta, _, _ = adapter("meta_responses", (200, body))
    reply = meta.complete(request(), bundle())
    assert reply.status == "failed" and "***" in reply.error.message
    _clean(reply)


@pytest.mark.parametrize("provider,body", [
    ("meta_responses", meta_response([meta_text(f"echo {SECRET}")])),
    ("meta_responses", meta_response([meta_call(arguments=json.dumps({"k": SECRET}))])),
    ("meta_responses", {**meta_response([]), "status": f"weird-{SECRET}"}),
    (BASETEN, chat_response(f"echo {SECRET}")),
    (BASETEN, chat_response(tool_calls=[chat_call(arguments=json.dumps({"k": SECRET}))],
                            finish="tool_calls")),
    (BASETEN, chat_response(finish=f"odd-{SECRET}")),
], ids=["meta-text", "meta-args", "meta-invalid-status", "chat-text", "chat-args",
        "chat-invalid-finish"])
def test_echoed_credential_is_scrubbed_on_every_reply_path(provider, body):
    client, _, _ = adapter(provider, (200, body))
    _clean(client.complete(request(provider), bundle()))


# --- finding 6: response size and deadline are enforced while reading -----------------------

class CountingStream:
    """A lazily produced body that records how many bytes the client consumed."""

    def __init__(self, total, chunk=65_536, on_chunk=None):
        self.total, self.chunk, self.sent, self.on_chunk = total, chunk, 0, on_chunk

    def __iter__(self):
        while self.sent < self.total:
            size = min(self.chunk, self.total - self.sent)
            self.sent += size
            if self.on_chunk:
                self.on_chunk()
            yield b"x" * size


def streamed(status, stream, headers=None):
    return lambda _request: httpx.Response(status, content=stream, headers=headers or {})


def test_oversized_success_body_is_rejected_without_full_download():
    stream = CountingStream(5_000_000)
    meta, _, _ = adapter("meta_responses", streamed(200, stream))
    reply = meta.complete(request(), bundle())
    assert reply.status == "failed" and reply.error.code == "provider_invalid_output"
    assert "too large" in reply.error.message
    assert stream.sent <= MAX_RESPONSE_BYTES + stream.chunk < 5_000_000


def test_declared_oversized_body_is_rejected_before_reading():
    stream = CountingStream(5_000_000)
    meta, _, _ = adapter("meta_responses",
                         streamed(200, stream, {"content-length": "5000000"}))
    reply = meta.complete(request(), bundle())
    assert reply.error.code == "provider_invalid_output" and stream.sent == 0


def test_error_bodies_are_read_with_a_small_bound():
    stream = CountingStream(5_000_000)
    meta, _, _ = adapter("meta_responses", streamed(400, stream),
                         cfg=config(retry=RetryPolicy(max_attempts=1)))
    reply = meta.complete(request(), bundle())
    assert reply.status == "failed" and reply.error.code == "provider_permanent"
    assert stream.sent <= MAX_ERROR_BODY_BYTES + stream.chunk


def test_absolute_deadline_is_enforced_during_the_read():
    fake = FakeTime()
    stream = CountingStream(400_000, on_chunk=lambda: setattr(fake, "now", fake.now + 30))
    meta, _, _ = adapter("meta_responses", streamed(200, stream), fake=fake)
    reply = meta.complete(request(timeout_ms=60_000), bundle())
    assert reply.status == "failed" and reply.error.code == "deadline_exceeded"
    assert stream.sent < 400_000


def test_normal_sized_streamed_body_still_parses():
    body = json.dumps(meta_response([meta_text(PROPOSAL)])).encode()
    meta, _, _ = adapter("meta_responses",
                         lambda _r: httpx.Response(200, content=iter([body[:10], body[10:]])))
    reply = meta.complete(request(), bundle())
    assert reply.status == "completed" and reply.structured_output == json.loads(PROPOSAL)
    assert reply.model == MODEL  # fixture model id only; no real model is involved


# --- re-review: credentials must not survive inside the opaque continuation ---------------

from ghostlab.providers.baseten_chat import PREFIX as CHAT_PREFIX  # noqa: E402
from ghostlab.providers.meta_responses import PREFIX as META_PREFIX  # noqa: E402
from ghostlab.providers.replies import decode_continuation  # noqa: E402


def _no_secret(value):
    text = json.dumps(value)
    assert SECRET not in text and json.dumps(SECRET)[1:-1] not in text
    return text


@pytest.mark.parametrize("echo", ["text", "arguments"])
def test_baseten_echoed_credential_never_enters_history_or_next_request(echo):
    if echo == "text":
        first = chat_response(f"the key is {SECRET}")
    else:
        first = chat_response(tool_calls=[chat_call("call_echo", arguments=json.dumps(
            {"order_id": "synthetic-order-1", "note": SECRET}))], finish="tool_calls")
    follow = chat_response("done")
    baseten, recorder, _ = adapter(BASETEN, (200, first), (200, follow))
    plain = {"output_schema": None}  # free-text turns, so the echoed text is a valid reply
    reply = baseten.complete(request(BASETEN, **plain), bundle())
    assert reply.status == ("completed" if echo == "text" else "tool_calls")
    _clean(reply)
    history = decode_continuation(CHAT_PREFIX, reply.continuation)
    _no_secret(history)
    assert "***" in json.dumps(history)  # masked, not silently dropped
    results = ([{"provider_call_id": "call_echo", "output_json": "{}", "is_error": False}]
               if echo == "arguments" else [])
    second = baseten.complete(request(BASETEN, continuation=reply.continuation,
                                      tool_results=results, **plain), bundle())
    assert second.status == "completed"
    sent = json.loads(recorder.requests[1].content)  # body only; the auth header carries the key
    _no_secret(sent)
    assert any("***" in json.dumps(m) for m in sent["messages"])


def test_meta_continuation_state_is_masked_too():
    meta, _, _ = adapter("meta_responses",
                         (200, meta_response([meta_call(f"call_{SECRET}"[:64])])))
    reply = meta.complete(request(), bundle())
    _clean(reply)
    _no_secret(decode_continuation(META_PREFIX, reply.continuation))
