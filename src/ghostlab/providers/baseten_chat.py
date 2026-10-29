"""Conditional Baseten Chat Completions adapter. POST {base_url}/chat/completions.

A separate wire serializer, selected only by explicit configuration. From
Baseten's Chat Completions reference (cited in docs/modules/M01.md): nested
``{"type": "function", "function": {...}}`` tools, ``tool_calls`` with ``id`` and
``function.name``/``function.arguments`` (a JSON string), ``role: tool`` results
with ``tool_call_id``, ``max_tokens``, ``response_format`` json_schema, and
``prompt_tokens``/``completion_tokens`` usage, which the reference says may be
absent on non-streaming replies.

Chat Completions has no server-side continuation, so the prior assistant/tool
messages travel inside this adapter's opaque continuation token.
"""

from ghostlab.contracts import ContextBundle, ModelReply, ModelRequest

from . import render
from .base import ProviderConfig, ProviderRequestError, check_request, utc_now
from .http import Attempt, Runtime, post_json
from .replies import (InvalidOutput, as_count, build, check_pairing, check_tool_calls,
                      decode_continuation, encode_continuation, failed, invalid, scrub,
                      structured_output)

PREFIX = "baseten-chat:v1:"
FINISH = {"stop": "completed", "length": "incomplete", "tool_calls": "tool_calls",
          "content_filter": "refused"}
MAX_CONTINUATION = 65_536


class BasetenChatAdapter:
    def __init__(self, config: ProviderConfig, runtime: Runtime | None = None, clock=utc_now):
        if config.provider != "baseten_chat_completions":
            raise ValueError("BasetenChatAdapter requires a baseten_chat_completions config")
        self.config = config
        self.runtime = runtime or Runtime()
        self.clock = clock
        self.last_attempts: list[Attempt] = []

    def _history(self, request: ModelRequest) -> list[dict]:
        state = decode_continuation(PREFIX, request.continuation)
        history = [] if state is None else state.get("messages", [])
        if not isinstance(history, list) or not all(isinstance(m, dict) for m in history):
            raise ProviderRequestError("Continuation is malformed")
        pending = []
        if history and history[-1].get("role") == "assistant":
            pending = [call.get("id") for call in history[-1].get("tool_calls") or []]
        if request.tool_results or pending:
            if state is None:
                raise ProviderRequestError("Tool results need the continuation that issued them")
            check_pairing(pending, request)
        tool_messages = [{"role": "tool", "tool_call_id": r.provider_call_id,
                          "content": r.output_json} for r in request.tool_results]
        return history + tool_messages

    def build_payload(self, request: ModelRequest, bundle: ContextBundle) -> dict:
        check_request(self.config, request, bundle)
        history = self._history(request)
        payload = {"model": self.config.model,
                   "messages": [{"role": "system", "content": render.render_instructions(bundle)},
                                {"role": "user", "content": render.render_user_input(bundle)},
                                *history],
                   "max_tokens": request.max_output_tokens, "stream": False}
        if request.tools:
            payload["tools"] = []
            for tool in request.tools:
                parameters = render.tool_parameters(tool)
                payload["tools"].append({"type": "function", "function": {
                    "name": tool.name, "description": tool.description,
                    "parameters": parameters,
                    "strict": render.use_strict(parameters, self.config.strict_tools)}})
        schema = render.resolve_output_schema(request.output_schema)
        if schema is not None:
            name, json_schema, _ = schema
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": name, "schema": json_schema,
                "strict": not render.strict_subset_violations(json_schema)}}
        return payload

    def complete(self, request: ModelRequest, bundle: ContextBundle, *, cancel=None) -> ModelReply:
        return scrub(self.config, self._complete(request, bundle, cancel))

    def _complete(self, request, bundle, cancel) -> ModelReply:
        payload = self.build_payload(request, bundle)
        history = payload["messages"][2:]
        outcome = post_json(self.config, "/chat/completions", payload,
                            timeout_ms=request.timeout_ms, cancel=cancel, runtime=self.runtime)
        self.last_attempts = outcome.attempts
        received_at = self.clock()
        if outcome.error is not None:
            return failed(self.config, request, outcome.error, received_at)
        try:
            return self.parse(request, outcome.body, history, received_at)
        except InvalidOutput as error:
            return invalid(self.config, request, str(error), received_at,
                           usage=_usage(outcome.body))

    def parse(self, request, body: dict, history: list[dict], received_at: str) -> ModelReply:
        if not self.config.response_model_ok(body.get("model")):
            raise InvalidOutput("Provider reported a different model than requested")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise InvalidOutput("Response has no choices")
        choice = choices[0]
        # The reference's examples read choices[0].message; its schema names `delta`.
        message = choice.get("message", choice.get("delta"))
        if not isinstance(message, dict):
            raise InvalidOutput("Choice has no message")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise InvalidOutput("Message content is not a string")
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise InvalidOutput("tool_calls is not a list")
        calls, wire_calls = [], []
        for raw in raw_calls:
            function = raw.get("function") if isinstance(raw, dict) else None
            if not (isinstance(raw, dict) and isinstance(raw.get("id"), str) and raw["id"]
                    and isinstance(function, dict) and isinstance(function.get("name"), str)
                    and isinstance(function.get("arguments"), str)):
                raise InvalidOutput("Tool call needs id, function.name and string arguments")
            calls.append({"provider_call_id": raw["id"], "tool_name": function["name"],
                          "arguments_json": function["arguments"]})
            wire_calls.append({"id": raw["id"], "type": "function", "function": {
                "name": function["name"], "arguments": function["arguments"]}})
        check_tool_calls(calls)
        finish = choice.get("finish_reason")
        if finish not in FINISH:
            raise InvalidOutput(f"Unexpected finish_reason {finish!r}")
        status = FINISH[finish]
        if status == "tool_calls" and not calls:
            raise InvalidOutput("finish_reason tool_calls without tool calls")
        if status == "completed" and calls:
            status = "tool_calls"
        if status in ("incomplete", "refused"):
            # Truncated or filtered output: calls may be partial or blocked. Never expose
            # them as executable, and keep them out of the continuation history.
            calls, wire_calls = [], []
        text = content or None
        structured = None
        if status == "completed":
            structured = structured_output(
                text, render.resolve_output_schema(request.output_schema))
        assistant = {"role": "assistant", "content": content}
        if wire_calls:
            assistant["tool_calls"] = wire_calls
        continuation = encode_continuation(PREFIX, {"messages": [*history, assistant]})
        if len(continuation) > MAX_CONTINUATION:
            raise InvalidOutput("Conversation history exceeds the continuation limit")
        return build(self.config, request, status=status, text=text, tool_calls=calls,
                     usage=_usage(body), continuation=continuation, structured=structured,
                     received_at=received_at)


def _usage(body):
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    return {"input_tokens": as_count(usage.get("prompt_tokens")),
            "output_tokens": as_count(usage.get("completion_tokens")),
            "cost_minor": None, "cost_currency": None}
