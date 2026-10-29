"""Baseten Chat Completions adapter against reference-shaped FIXTURES (not live inference)."""

import json

import pytest

from m01_fixtures import (MODEL, PROPOSAL, SECRET, adapter, bundle, chat_call, chat_response,
                          example, meta_call, meta_response, request)
from ghostlab.providers import ProviderConfigError, ProviderRequestError

TOOL = example("tool_definition.issue_refund")
PROVIDER = "baseten_chat_completions"


def chat(*responses):
    return adapter(PROVIDER, *responses)


def test_request_shape_follows_chat_completions_reference():
    baseten, recorder, _ = chat((200, chat_response(PROPOSAL)))
    reply = baseten.complete(request(PROVIDER, tools=[TOOL]), bundle())
    assert reply.status == "completed" and reply.structured_output == json.loads(PROPOSAL)
    (sent,) = recorder.requests
    assert str(sent.url) == "https://inference.baseten.co/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {SECRET}"
    body = json.loads(sent.content)
    assert body["model"] == MODEL and body["max_tokens"] == 2000 and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    (tool,) = body["tools"]
    assert tool["type"] == "function" and set(tool) == {"type", "function"}
    assert tool["function"]["name"] == "issue_refund"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "experiment_proposal"


def test_tool_roundtrip_appends_assistant_then_tool_messages():
    baseten, recorder, _ = chat(
        (200, chat_response(None, [chat_call("call_1"), chat_call("call_2", "get_order")],
                            finish="tool_calls")),
        (200, chat_response(PROPOSAL)))
    first = baseten.complete(request(PROVIDER), bundle())
    assert first.status == "tool_calls" and first.continuation.startswith("baseten-chat:v1:")
    results = [{"provider_call_id": "call_2", "output_json": '{"paid_minor":250000}',
                "is_error": False},
               {"provider_call_id": "call_1", "output_json": '{"status":"not_found"}',
                "is_error": False}]
    second = baseten.complete(request(PROVIDER, continuation=first.continuation,
                                      tool_results=results), bundle())
    messages = recorder.bodies()[1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "tool"]
    assert [c["id"] for c in messages[2]["tool_calls"]] == ["call_1", "call_2"]
    assert [(m["tool_call_id"], m["content"]) for m in messages[3:]] == [
        ("call_2", '{"paid_minor":250000}'), ("call_1", '{"status":"not_found"}')]
    assert second.status == "completed"


def test_unpaired_results_and_foreign_continuations_rejected_without_request():
    baseten, recorder, _ = chat((200, chat_response(None, [chat_call("call_1")],
                                                    finish="tool_calls")))
    first = baseten.complete(request(PROVIDER), bundle())
    wrong = [{"provider_call_id": "call_x", "output_json": "{}", "is_error": False}]
    with pytest.raises(ProviderRequestError):
        baseten.complete(request(PROVIDER, continuation=first.continuation,
                                 tool_results=wrong), bundle())
    with pytest.raises(ProviderRequestError):  # pending call left unanswered
        baseten.complete(request(PROVIDER, continuation=first.continuation), bundle())
    meta, _, _ = adapter("meta_responses", (200, meta_response([meta_call()])))
    meta_token = meta.complete(request(), bundle()).continuation
    with pytest.raises(ProviderRequestError):
        baseten.complete(request(PROVIDER, continuation=meta_token), bundle())
    with pytest.raises(ProviderRequestError):
        baseten.complete(request(PROVIDER, continuation="baseten-chat:v1:!!!"), bundle())
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("finish,status", [("stop", "completed"), ("length", "incomplete"),
                                           ("content_filter", "refused")])
def test_finish_reason_mapping(finish, status):
    baseten, _, _ = chat((200, chat_response(PROPOSAL, finish=finish)))
    assert baseten.complete(request(PROVIDER), bundle()).status == status


@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}, (10, 4)),
    (None, (None, None)),  # reference: usage may be absent on non-streaming replies
    ({"prompt_tokens": False, "completion_tokens": "4"}, (None, None)),
], ids=["reported", "omitted", "malformed"])
def test_usage_omissions_stay_unknown(usage, expected):
    baseten, _, _ = chat((200, chat_response(PROPOSAL, usage=usage)))
    reply = baseten.complete(request(PROVIDER), bundle())
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == expected


def test_delta_shaped_choice_is_accepted():
    body = chat_response(PROPOSAL)
    body["choices"][0]["delta"] = body["choices"][0].pop("message")
    baseten, _, _ = chat((200, body))
    assert baseten.complete(request(PROVIDER), bundle()).status == "completed"


@pytest.mark.parametrize("body", [
    {"choices": []},
    chat_response(PROPOSAL, finish="mystery"),
    chat_response(None, [{"id": "c", "type": "function", "function": {"name": "x"}}],
                  finish="tool_calls"),
    chat_response(None, [chat_call("dup"), chat_call("dup")], finish="tool_calls"),
    chat_response(None, [], finish="tool_calls"),
    chat_response(["not", "a", "string"]),
    chat_response(PROPOSAL, model="silently-swapped-model"),
], ids=["no-choices", "unknown-finish", "no-arguments", "dup-ids", "empty-tool-calls",
        "content-list", "model-switch"])
def test_invalid_output_becomes_failed_reply(body):
    baseten, _, _ = chat((200, body))
    reply = baseten.complete(request(PROVIDER), bundle())
    assert reply.status == "failed" and reply.error.code == "provider_invalid_output"


def test_request_for_other_provider_or_model_is_refused():
    baseten, recorder, _ = chat()
    with pytest.raises(ProviderConfigError):
        baseten.complete(request("meta_responses"), bundle())
    with pytest.raises(ProviderConfigError):
        baseten.complete(request(PROVIDER, model="meta-models/other"), bundle())
    assert recorder.requests == []
