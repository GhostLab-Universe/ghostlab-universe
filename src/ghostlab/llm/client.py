"""Provider clients for multi-turn tool loops, on the ported transport.

One conversation interface serves every provider so one agent loop runs on all:

    conv = client.open(system, tools)        # tools: [{"name","description","parameters"}]
    turn = conv.send(user="...")             # first turn
    turn = conv.send(tool_results=[{"id", "output"}])

- ``ResponsesClient``: Meta Model API, POST /responses, reasoning carried across
  tool turns via ``previous_response_id`` (the probe-qualified wire format).
- ``ChatClient``: Chat Completions (local Ollama Muse Glimmer), full history resent.
- ``ReplayClient``: returns recorded turns, checks each turn's input digest, zero network.

Both live clients go through ``providers.http.post_json`` (bounded retry, redaction,
outbound policy) with the spend ledger as the per-attempt gate. A missing key or
unreachable provider is an explicit ``unavailable`` status; nothing is substituted.
"""

import dataclasses
import hashlib
import json
import os
import time
from dataclasses import dataclass, field

import httpx

from ..providers.base import Credential, ProviderConfig, ProviderConfigError, RetryPolicy
from ..providers.http import Runtime, post_json
from ..providers.outbound import OutboundPolicy
from .spend import SpendLedger, worst_case

META_DEFAULT_BASE = "https://api.meta.ai/v1"
GLIMMER_MODEL = "hf.co/bartowski/Muse-Glimmer-30B-GGUF:IQ2_M"
PROVIDERS = {
    # name: (key env var, key label, wire)
    "meta-dev": ("META_API_KEY_DEV", "dev", "responses"),
    "meta-public": ("META_API_KEY_PUBLIC", "public", "responses"),
    "glimmer-local": (None, "glimmer", "chat"),
}


class ProviderUnavailable(Exception):
    pass


class ModelCallFailed(Exception):
    """A live model call failed (transport, provider or spend refusal)."""


class ReplayDiverged(Exception):
    """Replay input no longer matches the recording: the run is not reproduced."""


@dataclass
class Turn:
    status: str  # completed | tool_calls | incomplete | failed
    text: str | None = None
    tool_calls: list = field(default_factory=list)  # [{"id","name","arguments": dict|None,"raw"}]
    usage: dict = field(default_factory=dict)
    request_id: str | None = None
    latency_ms: int | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None

    def to_dict(self):
        return dataclasses.asdict(self)


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


def _parse_args(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class _LiveClient:
    wire = ""

    def __init__(self, provider: str, config: ProviderConfig, *, label: str,
                 ledger: SpendLedger | None, max_output_tokens: int = 2048,
                 reasoning_effort: str | None = None, timeout_ms: int = 120_000,
                 usd_limit: float | None = None, transport=None, purpose: str | None = None):
        self.provider, self.config, self.label = provider, config, label
        self.ledger = ledger or SpendLedger()
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.timeout_ms = timeout_ms
        self.usd_limit = usd_limit  # per-session cap on top of the per-key cap
        self.session_usd = 0.0
        self.calls: list = []  # model-call log for the UI
        self.purpose = purpose
        self.runtime = Runtime(transport=transport,
                               outbound=OutboundPolicy(allow_server_retention=True))

    @property
    def model(self):
        return self.config.model

    def _post(self, path, payload) -> Turn:
        size = len(json.dumps(payload).encode())
        estimate = worst_case(self.model, size, self.max_output_tokens)
        refusal = []

        def gate():
            try:
                self.ledger.check(self.label, estimate)
            except Exception as error:  # SpendRefused
                refusal.append(str(error))
                return False
            if self.usd_limit is not None and self.session_usd + estimate > self.usd_limit:
                refusal.append(f"session cap {self.usd_limit} USD would be exceeded"
                               f" (spent {self.session_usd:.4f}, worst case {estimate:.4f})")
                return False
            return True

        runtime = dataclasses.replace(self.runtime, attempt_gate=gate)
        started = time.monotonic()
        outcome = post_json(self.config, path, payload, timeout_ms=self.timeout_ms,
                            cancel=None, runtime=runtime)
        latency = int((time.monotonic() - started) * 1000)
        if outcome.error is not None:
            message = refusal[0] if refusal else f"{outcome.error.code}: {outcome.error.message}"
            return Turn("failed", error=message, latency_ms=latency, provider=self.provider,
                        model=self.model)
        turn = self.parse(outcome.body)
        turn.latency_ms, turn.provider, turn.model = latency, self.provider, self.model
        u = turn.usage
        row = self.ledger.record(label=self.label, provider=self.provider, model=self.model,
                                 request_id=turn.request_id,
                                 input_tokens=u.get("input_tokens"),
                                 output_tokens=u.get("output_tokens"),
                                 reasoning_tokens=u.get("reasoning_tokens"),
                                 cached_tokens=u.get("cached_tokens"), latency_ms=latency,
                                 purpose=self.purpose)
        u["usd"] = row["usd"]
        self.session_usd += row["usd"]
        self.calls.append({"provider": self.provider, "model": self.model,
                           "request_id": turn.request_id, "latency_ms": latency,
                           "input_tokens": u.get("input_tokens"),
                           "output_tokens": u.get("output_tokens"),
                           "reasoning_tokens": u.get("reasoning_tokens"), "usd": row["usd"],
                           "status": turn.status})
        return turn


class ResponsesClient(_LiveClient):
    wire = "responses"

    def open(self, system, tools):
        return _ResponsesConversation(self, system, tools)

    def parse(self, body) -> Turn:
        texts, calls = [], []
        for item in body.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for block in item.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        texts.append(str(block.get("text", "")))
            elif item.get("type") == "function_call":
                calls.append({"id": str(item.get("call_id")), "name": item.get("name"),
                              "arguments": _parse_args(item.get("arguments")),
                              "raw": item.get("arguments")})
        usage = body.get("usage") or {}
        status = body.get("status")
        if status == "completed":
            status = "tool_calls" if calls else "completed"
        elif status == "incomplete":
            calls = []  # truncated output: proposed calls may be partial, none are exposed
        else:
            status = "failed"
        return Turn(status, text="".join(texts) or None, tool_calls=calls,
                    request_id=body.get("id"),
                    usage={"input_tokens": usage.get("input_tokens"),
                           "output_tokens": usage.get("output_tokens"),
                           "reasoning_tokens": (usage.get("output_tokens_details") or {})
                           .get("reasoning_tokens"),
                           "cached_tokens": (usage.get("input_tokens_details") or {})
                           .get("cached_tokens")},
                    error=None if status != "failed" else f"response status {body.get('status')}")


class _ResponsesConversation:
    def __init__(self, client, system, tools):
        self.client, self.system = client, system
        self.tools = [{"type": "function", "name": t["name"], "description": t["description"],
                       "parameters": t["parameters"]} for t in tools]
        self.previous = None

    def send(self, user=None, tool_results=None) -> Turn:
        payload = {"model": self.client.model, "instructions": self.system,
                   "max_output_tokens": self.client.max_output_tokens, "store": True,
                   "tools": self.tools}
        if self.client.reasoning_effort:
            payload["reasoning"] = {"effort": self.client.reasoning_effort}
        if tool_results:
            payload["input"] = [{"type": "function_call_output", "call_id": r["id"],
                                 "output": r["output"]} for r in tool_results]
        else:
            payload["input"] = [{"role": "user", "content": [
                {"type": "input_text", "text": user}]}]
        if self.previous:
            payload["previous_response_id"] = self.previous
        turn = self.client._post("/responses", payload)
        if turn.request_id:
            self.previous = turn.request_id
        return turn


class ChatClient(_LiveClient):
    wire = "chat"

    def open(self, system, tools):
        return _ChatConversation(self, system, tools)

    def parse(self, body) -> Turn:
        choices = body.get("choices") or [{}]
        message = choices[0].get("message") or {}
        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function") or {}
            calls.append({"id": call.get("id") or f"call_{index}", "name": fn.get("name"),
                          "arguments": _parse_args(fn.get("arguments")),
                          "raw": fn.get("arguments")})
        usage = body.get("usage") or {}
        finish = choices[0].get("finish_reason")
        status = "tool_calls" if calls else ("incomplete" if finish == "length" else "completed")
        return Turn(status, text=message.get("content") or None, tool_calls=calls,
                    request_id=body.get("id"),
                    usage={"input_tokens": usage.get("prompt_tokens"),
                           "output_tokens": usage.get("completion_tokens"),
                           "reasoning_tokens": None, "cached_tokens": None})


class _ChatConversation:
    def __init__(self, client, system, tools):
        self.client = client
        self.tools = [{"type": "function", "function": t} for t in tools]
        self.messages = [{"role": "system", "content": system}]

    def send(self, user=None, tool_results=None) -> Turn:
        if tool_results:
            self.messages += [{"role": "tool", "tool_call_id": r["id"], "content": r["output"]}
                              for r in tool_results]
        else:
            self.messages.append({"role": "user", "content": user})
        payload = {"model": self.client.model, "messages": self.messages, "tools": self.tools,
                   "max_tokens": self.client.max_output_tokens, "temperature": 0}
        turn = self.client._post("/chat/completions", payload)
        if turn.status != "failed":
            assistant = {"role": "assistant", "content": turn.text or ""}
            if turn.tool_calls:
                assistant["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                    "name": c["name"], "arguments": c["raw"] if isinstance(c["raw"], str)
                    else json.dumps(c["raw"])}} for c in turn.tool_calls]
            self.messages.append(assistant)
        return turn


class RecordingClient:
    """Wraps a client; records every turn and the digest of the input that produced it."""

    def __init__(self, inner):
        self.inner = inner
        self.records: list = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def open(self, system, tools):
        conv = self.inner.open(system, tools)
        head = {"system": system, "tools": tools}
        recorder = self

        class _Conv:
            def send(self, user=None, tool_results=None):
                turn = conv.send(user=user, tool_results=tool_results)
                recorder.records.append({"input_digest": digest(
                    {**head, "user": user, "tool_results": tool_results}),
                    "turn": turn.to_dict()})
                return turn
        return _Conv()


class ReplayClient:
    """Feeds recorded turns back in order. Zero network, zero keys."""

    def __init__(self, records, provider="replay", model="replay"):
        self.records = list(records)
        self.provider, self.model = provider, model
        self.calls: list = []
        self.session_usd = 0.0
        self._next = 0

    def open(self, system, tools):
        head = {"system": system, "tools": tools}
        replay = self

        class _Conv:
            def send(self, user=None, tool_results=None):
                if replay._next >= len(replay.records):
                    raise ReplayDiverged("replay asked for more model turns than recorded")
                record = replay.records[replay._next]
                replay._next += 1
                got = digest({**head, "user": user, "tool_results": tool_results})
                if got != record["input_digest"]:
                    raise ReplayDiverged(f"turn {replay._next}: input differs from recording")
                turn = Turn(**record["turn"])
                replay.calls.append({"provider": turn.provider, "model": turn.model,
                                     "request_id": turn.request_id, "latency_ms": 0,
                                     "input_tokens": turn.usage.get("input_tokens"),
                                     "output_tokens": turn.usage.get("output_tokens"),
                                     "reasoning_tokens": turn.usage.get("reasoning_tokens"),
                                     "usd": 0.0, "status": turn.status, "replayed": True})
                return turn
        return _Conv()

    @property
    def exhausted(self):
        return self._next == len(self.records)


def provider_status(name: str, *, timeout_s: float = 2.0) -> dict:
    """``available`` or ``unavailable`` with a reason. Never reveals key values."""
    if name not in PROVIDERS:
        return {"provider": name, "status": "unavailable", "reason": "unknown provider"}
    key_var, label, _ = PROVIDERS[name]
    if key_var is not None:
        if not os.environ.get(key_var, "").strip():
            return {"provider": name, "status": "unavailable", "reason": f"{key_var} not set"}
        return {"provider": name, "status": "available", "key_label": label}
    base = os.environ.get("GLIMMER_BASE_URL", "").strip()
    if not base:
        return {"provider": name, "status": "unavailable", "reason": "GLIMMER_BASE_URL not set"}
    try:
        models = httpx.get(base.rstrip("/") + "/models", timeout=timeout_s).json()
        ids = {m.get("id") for m in models.get("data", [])}
    except Exception as error:
        return {"provider": name, "status": "unavailable",
                "reason": f"unreachable: {type(error).__name__}"}
    if GLIMMER_MODEL not in ids:
        return {"provider": name, "status": "unavailable", "reason": f"{GLIMMER_MODEL} not pulled"}
    return {"provider": name, "status": "available", "key_label": label}


def client_for(name: str, model: str | None = None, *, ledger: SpendLedger | None = None,
               transport=None, check_status: bool = True, **options):
    """Live client for exactly the named provider, or ProviderUnavailable. No fallback."""
    if name not in PROVIDERS:
        raise ProviderUnavailable(f"unknown provider {name!r}")
    key_var, label, wire = PROVIDERS[name]
    if check_status:
        status = provider_status(name)
        if status["status"] != "available":
            raise ProviderUnavailable(f"{name}: {status['reason']}")
    try:
        if wire == "responses":
            base = os.environ.get("META_BASE_URL", "").strip()
            config = ProviderConfig(provider="meta_responses", model=model or "muse-spark-1.3",
                                    credential=Credential.from_env(key_var),
                                    base_url=base if base.startswith("https://")
                                    else META_DEFAULT_BASE)
            return ResponsesClient(name, config, label=label, ledger=ledger,
                                   transport=transport, **options)
        # ponytail: Ollama ignores auth; a fixed non-secret placeholder satisfies Credential.
        config = ProviderConfig(provider="glimmer_local", model=model or GLIMMER_MODEL,
                                credential=Credential("ollama-local-no-auth", "none"),
                                base_url=os.environ.get("GLIMMER_BASE_URL", "").strip(),
                                retry=RetryPolicy(max_attempts=1))
        options.setdefault("timeout_ms", 300_000)
        return ChatClient(name, config, label=label, ledger=ledger, transport=transport,
                          **options)
    except ProviderConfigError as error:
        raise ProviderUnavailable(f"{name}: {error}") from error
