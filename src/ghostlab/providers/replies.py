"""Normalize parsed provider output into a validated M00 ModelReply."""

import base64
import json

from pydantic import ValidationError

from ghostlab.contracts import ErrorInfo, ModelReply, ModelRequest

from .base import ProviderConfig, ProviderRequestError

MAX_TOOL_CALLS = 16


class InvalidOutput(Exception):
    """The provider answered, but not in a shape this adapter can trust."""


def as_count(value):
    """Provider token counts; anything missing or malformed is unknown (None), never 0."""
    return value if type(value) is int and value >= 0 else None


def failed(config: ProviderConfig, request: ModelRequest, error: ErrorInfo, received_at: str):
    return ModelReply.model_validate({
        "schema_version": 1, "request_id": request.request_id, "provider": config.provider,
        "model": config.model, "status": "failed", "text": None, "tool_calls": [],
        "structured_output": None,
        "usage": {"input_tokens": None, "output_tokens": None, "cost_minor": None,
                  "cost_currency": None},
        "continuation": None, "error": error.model_dump(), "received_at": received_at})


def scrub(config: ProviderConfig, reply: ModelReply) -> ModelReply:
    """Remove the configured credential from every field of a reply.

    Provider bodies (error messages, text, tool arguments) can echo a key back.
    Each adapter passes every reply through here, so no path can leak it.
    """
    secret = config.credential.reveal()
    serialized = reply.model_dump_json()
    escaped = json.dumps(secret)[1:-1]
    if secret not in serialized and escaped not in serialized:
        return reply
    return ModelReply.model_validate_json(
        serialized.replace(escaped, "***").replace(secret, "***"))


def invalid(config, request, message: str, received_at: str, usage=None):
    reply = failed(config, request, ErrorInfo(
        code="provider_invalid_output", message=message[:500], retryable=False,
        side_effect="none", ref=None), received_at)
    if usage is None:
        return reply
    return ModelReply.model_validate({**reply.model_dump(), "usage": usage})


def build(config, request, *, status, text, tool_calls, usage, continuation, structured,
          received_at) -> ModelReply:
    try:
        return ModelReply.model_validate({
            "schema_version": 1, "request_id": request.request_id,
            "provider": config.provider, "model": config.model, "status": status,
            "text": text, "tool_calls": tool_calls, "structured_output": structured,
            "usage": usage, "continuation": continuation, "error": None,
            "received_at": received_at})
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise InvalidOutput(f"Reply violates model_reply/1 at {location}: {first['type']}")


def check_tool_calls(calls: list[dict]):
    if len(calls) > MAX_TOOL_CALLS:
        raise InvalidOutput(f"More than {MAX_TOOL_CALLS} tool calls in one reply")
    ids = [call["provider_call_id"] for call in calls]
    if len(set(ids)) != len(ids):
        raise InvalidOutput("Duplicate provider call ids")


def structured_output(text, schema):
    """Parse and locally validate structured output against its M00 contract."""
    if schema is None:
        return None
    name, _, model = schema
    if not text:
        raise InvalidOutput(f"Expected {name} structured output; reply has no text")
    try:
        value = json.loads(text)
    except ValueError:
        raise InvalidOutput(f"Structured output for {name} is not JSON")
    if not isinstance(value, dict):
        raise InvalidOutput(f"Structured output for {name} is not an object")
    try:
        model.model_validate_json(text)
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "$"
        raise InvalidOutput(f"Structured output fails {name} at {location}: {first['type']}")
    return value


def encode_continuation(prefix: str, state: dict) -> str:
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
    return prefix + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_continuation(prefix: str, token: str | None) -> dict | None:
    """Continuations are opaque outside this package and bound to one provider."""
    if token is None:
        return None
    if not token.startswith(prefix):
        raise ProviderRequestError("Continuation was issued by a different provider adapter")
    body = token[len(prefix):]
    try:
        state = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, UnicodeDecodeError):
        raise ProviderRequestError("Continuation is malformed")
    if not isinstance(state, dict):
        raise ProviderRequestError("Continuation is malformed")
    return state


def check_pairing(pending: list[str], request: ModelRequest):
    """Every pending provider call needs exactly one result, and nothing else."""
    results = [result.provider_call_id for result in request.tool_results]
    if len(set(results)) != len(results):
        raise ProviderRequestError("Duplicate tool results for one provider call id")
    if set(results) != set(pending):
        missing = sorted(set(pending) - set(results))
        unknown = sorted(set(results) - set(pending))
        raise ProviderRequestError(
            f"Tool results do not pair with pending calls (missing={missing}, unknown={unknown})")
