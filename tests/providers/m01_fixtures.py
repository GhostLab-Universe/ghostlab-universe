"""Offline test fixtures for M01. Every provider response here is a FIXTURE shaped from
the cited documentation, not output from a live model."""

import json
from pathlib import Path

import httpx

from ghostlab.contracts import ContextBundle, ModelRequest
from ghostlab.providers import (BasetenChatAdapter, Credential, MetaResponsesAdapter,
                                ProviderConfig, RetryPolicy, Runtime)

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-fixture-SECRET-0123456789abcdef"
NOW = "2026-10-29T12:00:00.000000Z"
MODEL = "fixture-model"


def example(name: str) -> dict:
    return json.loads((ROOT / "examples" / "contracts" / f"{name}.json").read_text("utf-8"))


def bundle(**changes) -> ContextBundle:
    data = example("context_bundle.explorer")
    data.update(changes)
    return ContextBundle.model_validate(data)


def request(provider="meta_responses", **changes) -> ModelRequest:
    data = example("model_request.explorer")
    data.update(provider=provider, **changes)
    return ModelRequest.model_validate(data)


def config(provider="meta_responses", **changes) -> ProviderConfig:
    base = {"meta_responses": "https://api.meta.ai/v1",
            "baseten_chat_completions": "https://inference.baseten.co/v1"}[provider]
    values = {"provider": provider, "model": MODEL, "base_url": base,
              "credential": Credential(SECRET, "test-fixture"),
              "retry": RetryPolicy(max_attempts=3)}
    values.update(changes)
    return ProviderConfig(**values)


class FakeTime:
    """Monotonic clock that only advances when the adapter sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Recorder:
    """httpx.MockTransport handler that replays scripted responses and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, http_request: httpx.Request):
        self.requests.append(http_request)
        if not self.responses:
            raise AssertionError("Unexpected extra provider request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(http_request)
        status, body, headers = item if len(item) == 3 else (*item, {})
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(status, content=content, headers=headers)

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def adapter(provider="meta_responses", *responses, cfg=None, fake=None):
    recorder = Recorder(*responses)
    fake = fake or FakeTime()
    runtime = Runtime(transport=httpx.MockTransport(recorder), sleep=fake.sleep,
                      monotonic=fake.monotonic, jitter=lambda: 0.0)
    cls = MetaResponsesAdapter if provider == "meta_responses" else BasetenChatAdapter
    return cls(cfg or config(provider), runtime, clock=lambda: NOW), recorder, fake


def meta_response(output, *, status="completed", usage=None, response_id="resp_fixture1",
                  model=MODEL):
    body = {"id": response_id, "object": "response", "status": status, "model": model,
            "output": output}
    if usage is not None:
        body["usage"] = usage
    return body


def meta_text(text):
    return {"type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def meta_call(call_id="call_fixture_1", name="get_refund_status", arguments='{"order_id":"o"}'):
    return {"type": "function_call", "call_id": call_id, "name": name,
            "arguments": arguments, "status": "completed"}


def chat_response(content=None, tool_calls=None, finish="stop", usage=None, model=MODEL):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body = {"id": "chatcmpl-fixture", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = usage
    return body


def chat_call(call_id="call_fixture_1", name="get_refund_status", arguments='{"order_id":"o"}'):
    return {"id": call_id, "type": "function", "function": {"name": name,
                                                            "arguments": arguments}}


PROPOSAL = json.dumps(example("experiment_proposal.timeout_after_commit"))
