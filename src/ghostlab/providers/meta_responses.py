"""Direct Meta Responses adapter (primary). POST {base_url}/responses.

Wire format from Meta's Responses, tool-calling, structured-output and
error-handling docs (cited in docs/modules/M01.md): flat function tools,
``function_call`` output items, ``function_call_output`` inputs paired by
``call_id`` (1-64 chars) with ``previous_response_id``, instructions resent
every turn, usage ``input_tokens``/``output_tokens``. The Responses
``text.format`` json_schema field layout (name/schema/strict) is not spelled
out on the structured-output page and is marked unverified until probed.
"""

from ghostlab.contracts import ContextBundle, ModelReply, ModelRequest

from . import render
from .base import ProviderConfig, ProviderRequestError, check_request, utc_now
from .http import Attempt, Runtime, make_error, post_json
from .replies import (InvalidOutput, as_count, build, check_pairing, check_tool_calls,
                      decode_continuation, encode_continuation, failed, invalid, scrub,
                      structured_output)

PREFIX = "meta-responses:v1:"
MIN_OUTPUT_TOKENS = 16  # documented minimum for max_output_tokens


class MetaResponsesAdapter:
    def __init__(self, config: ProviderConfig, runtime: Runtime | None = None, clock=utc_now):
        if config.provider != "meta_responses":
            raise ValueError("MetaResponsesAdapter requires a meta_responses config")
        self.config = config
        self.runtime = runtime or Runtime()
        self.clock = clock
        self.last_attempts: list[Attempt] = []

    def build_payload(self, request: ModelRequest, bundle: ContextBundle) -> dict:
        check_request(self.config, request, bundle)
        if request.max_output_tokens < MIN_OUTPUT_TOKENS:
            raise ProviderRequestError("Meta requires max_output_tokens >= 16")
        state = decode_continuation(PREFIX, request.continuation)
        payload = {"model": self.config.model,
                   "instructions": render.render_instructions(bundle),
                   "max_output_tokens": request.max_output_tokens}
        if request.tool_results:
            if state is None:
                raise ProviderRequestError("Tool results need the continuation that issued them")
            check_pairing(state.get("pending_call_ids", []), request)
            payload["input"] = [{"type": "function_call_output", "call_id": r.provider_call_id,
                                 "output": r.output_json} for r in request.tool_results]
        else:
            if state is not None and state.get("pending_call_ids"):
                raise ProviderRequestError("Pending tool calls must be answered first")
            payload["input"] = [{"role": "user", "content": [
                {"type": "input_text", "text": render.render_user_input(bundle)}]}]
        if state is not None:
            payload["previous_response_id"] = state["response_id"]
        if request.tools:
            payload["tools"] = []
            for tool in request.tools:
                parameters = render.tool_parameters(tool)
                payload["tools"].append({
                    "type": "function", "name": tool.name, "description": tool.description,
                    "parameters": parameters,
                    "strict": render.use_strict(parameters, self.config.strict_tools)})
        schema = render.resolve_output_schema(request.output_schema)
        if schema is not None:
            name, json_schema, _ = schema
            strict = not render.strict_subset_violations(json_schema)
            payload["text"] = {"format": {"type": "json_schema", "name": name,
                                          "schema": json_schema, "strict": strict}}
        return payload

    def complete(self, request: ModelRequest, bundle: ContextBundle, *, cancel=None) -> ModelReply:
        return scrub(self.config, self._complete(request, bundle, cancel))

    def _complete(self, request, bundle, cancel) -> ModelReply:
        payload = self.build_payload(request, bundle)
        outcome = post_json(self.config, "/responses", payload, timeout_ms=request.timeout_ms,
                            cancel=cancel, runtime=self.runtime)
        self.last_attempts = outcome.attempts
        received_at = self.clock()
        if outcome.error is not None:
            return failed(self.config, request, outcome.error, received_at)
        try:
            return self.parse(request, outcome.body, received_at)
        except InvalidOutput as error:
            return invalid(self.config, request, str(error), received_at,
                           usage=_usage(outcome.body))

    def parse(self, request: ModelRequest, body: dict, received_at: str) -> ModelReply:
        if not self.config.response_model_ok(body.get("model")):
            raise InvalidOutput("Provider reported a different model than requested")
        output, status = body.get("output"), body.get("status")
        if not isinstance(output, list) or not isinstance(status, str):
            raise InvalidOutput("Response lacks output/status")
        texts, calls = [], []
        for item in output:
            if not isinstance(item, dict):
                raise InvalidOutput("Output item is not an object")
            if item.get("type") == "message":
                for block in item.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        if not isinstance(block.get("text"), str):
                            raise InvalidOutput("output_text without text")
                        texts.append(block["text"])
            elif item.get("type") == "function_call":
                call_id, name, arguments = (item.get("call_id"), item.get("name"),
                                            item.get("arguments"))
                if not (isinstance(call_id, str) and 1 <= len(call_id) <= 64):
                    raise InvalidOutput("function_call call_id must be 1-64 characters")
                if not isinstance(name, str) or not isinstance(arguments, str):
                    raise InvalidOutput("function_call needs string name and arguments")
                calls.append({"provider_call_id": call_id, "tool_name": name,
                              "arguments_json": arguments})
            # reasoning and other item types are never surfaced
        check_tool_calls(calls)
        response_id = body.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise InvalidOutput("Response has no id")
        text = "".join(texts) or None
        usage = _usage(body)
        if status == "completed":
            reply_status = "tool_calls" if calls else "completed"
        elif status == "incomplete":
            # Truncated output: any proposed calls may be partial, so none are exposed.
            reply_status, calls = "incomplete", []
        elif status in ("failed", "cancelled"):
            return failed(self.config, request,
                          _status_error(body, status, self.config.credential.reveal()),
                          received_at)
        else:
            raise InvalidOutput(f"Unexpected response status {status!r}")
        structured = None
        if reply_status == "completed":
            structured = structured_output(
                text, render.resolve_output_schema(request.output_schema))
        continuation = encode_continuation(PREFIX, {
            "response_id": response_id,
            "pending_call_ids": [call["provider_call_id"] for call in calls]},
            secrets=(self.config.credential.reveal(),))
        return build(self.config, request, status=reply_status, text=text, tool_calls=calls,
                     usage=usage, continuation=continuation, structured=structured,
                     received_at=received_at)


def _usage(body):
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    return {"input_tokens": as_count(usage.get("input_tokens")),
            "output_tokens": as_count(usage.get("output_tokens")),
            "cost_minor": None, "cost_currency": None}


def _status_error(body, status, secret):
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = error.get("code") if isinstance(error.get("code"), str) else None
    if status == "cancelled":
        return make_error("cancelled", "Provider reported the response as cancelled")
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    return make_error("provider_transient" if code == "server_error" else "provider_permanent",
                      f"Provider status failed {code or ''}: {message}".strip(),
                      retryable=code == "server_error", secrets=(secret,))
