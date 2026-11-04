"""Provider boundary shared by the Meta Responses and Baseten Chat adapters.

Interface version ``ghostlab-providers/1``. Adapters consume an M00
``ModelRequest`` plus its ``ContextBundle`` and return an M00 ``ModelReply``.
They never dispatch tools, never change provider or model, and never expose
credentials. Transport and provider failures come back as ``status="failed"``
replies with a typed ``ErrorInfo``; misconfiguration and malformed requests
raise before any network call.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from ghostlab.contracts import ContextBundle, ModelReply, ModelRequest

PROVIDER_INTERFACE_VERSION = "ghostlab-providers/1"

ProviderName = Literal["meta_responses", "baseten_chat_completions", "glimmer_local"]
# Local Ollama (Muse Glimmer). Not in ENV: it has no key and no probe; see ProviderConfig.
GLIMMER_PROVIDER = "glimmer_local"

# Documented base URLs (docs/modules/M01.md cites the sources). Models are never defaulted.
META_DEFAULT_BASE_URL = "https://api.meta.ai/v1"
BASETEN_DEFAULT_BASE_URL = "https://inference.baseten.co/v1"

ENV = {
    "meta_responses": ("MODEL_API_KEY", "GHOSTLAB_META_BASE_URL", "GHOSTLAB_META_MODEL_ID",
                       META_DEFAULT_BASE_URL),
    "baseten_chat_completions": ("BASETEN_API_KEY", "GHOSTLAB_BASETEN_BASE_URL",
                                 "GHOSTLAB_BASETEN_MODEL_ID", BASETEN_DEFAULT_BASE_URL),
}


class ProviderConfigError(Exception):
    """Misconfiguration or a provider/model mismatch. Raised before any network call."""


class ProviderRequestError(Exception):
    """A request this adapter cannot serialize faithfully. Raised before any network call."""


class Credential:
    """An API key that never appears in repr, str, logs or error messages."""

    __slots__ = ("_value", "source")

    def __init__(self, value: str, source: str):
        if not isinstance(value, str) or not value.strip():
            raise ProviderConfigError(f"Credential from {source} is empty")
        self._value = value
        self.source = source

    @classmethod
    def from_env(cls, name: str) -> "Credential":
        value = os.environ.get(name, "")
        if not value.strip():
            raise ProviderConfigError(f"Environment variable {name} is not set")
        return cls(value, f"env:{name}")

    def reveal(self) -> str:
        return self._value

    def __repr__(self):
        return f"Credential(source={self.source!r}, value=***)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("Credentials cannot be pickled")


_BEARER = re.compile(r"(?i)(bearer|api-key)\s+[A-Za-z0-9._~+/=-]{8,}")


def redact(text: str, *secrets: str) -> str:
    """Remove known secret values and Authorization-style tokens from text."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return _BEARER.sub(lambda m: f"{m.group(1)} ***", text)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry for transient provider errors only (429/500/502/503, connect errors)."""

    max_attempts: int = 3
    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0
    retry_after_max_s: float = 60.0

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ProviderConfigError("max_attempts must be an integer from 1 to 5")


@dataclass(frozen=True)
class ProviderConfig:
    """Explicit adapter configuration. One config serves exactly one provider and model."""

    provider: ProviderName
    model: str
    credential: Credential = field(repr=False)
    base_url: str
    retry: RetryPolicy = RetryPolicy()
    strict_tools: bool = True
    # Response "model" values accepted as the requested model (exact echo by default).
    accepted_response_models: frozenset[str] = frozenset()

    def __post_init__(self):
        if self.provider not in ENV and self.provider != GLIMMER_PROVIDER:
            raise ProviderConfigError("Unknown provider")
        if not isinstance(self.model, str) or not self.model:
            raise ProviderConfigError("An exact model identifier is required")
        parts = urlsplit(self.base_url)
        if not parts.netloc or parts.query or parts.fragment:
            raise ProviderConfigError("base_url must be an https URL without query or fragment")
        if parts.scheme == "https":
            return
        # The one http exception: local Ollama on loopback or the WSL host gateway.
        if not (self.provider == GLIMMER_PROVIDER and parts.scheme == "http"
                and parts.hostname in local_hosts()):
            raise ProviderConfigError("base_url must be an https URL without query or fragment")

    @classmethod
    def from_env(cls, provider: ProviderName, **overrides) -> "ProviderConfig":
        key_var, url_var, model_var, default_url = ENV[provider]
        model = os.environ.get(model_var, "").strip()
        if not model:
            raise ProviderConfigError(f"Environment variable {model_var} is not set")
        return cls(provider=provider, model=model, credential=Credential.from_env(key_var),
                   base_url=os.environ.get(url_var, "").strip() or default_url, **overrides)

    def response_model_ok(self, model) -> bool:
        return model is None or model == self.model or model in self.accepted_response_models


def default_gateway_ip() -> str | None:
    """IPv4 default gateway from /proc/net/route (the Windows host under WSL), or None."""
    try:
        with open("/proc/net/route", encoding="ascii") as handle:
            for line in handle.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    raw = bytes.fromhex(fields[2])[::-1]
                    return ".".join(str(b) for b in raw)
    except (OSError, ValueError):
        return None
    return None


def local_hosts() -> set:
    hosts = {"127.0.0.1", "localhost", "::1"}
    gateway = default_gateway_ip()
    if gateway:
        hosts.add(gateway)
    return hosts


@runtime_checkable
class CancelHook(Protocol):
    """Minimal cancellation hook (``threading.Event`` satisfies it).

    PROPOSAL for M05: M05 owns cancellation and deadlines; until it publishes
    its handle, adapters accept anything with ``is_set()``.
    """

    def is_set(self) -> bool: ...


@runtime_checkable
class ProviderAdapter(Protocol):
    config: ProviderConfig

    def complete(self, request: ModelRequest, bundle: ContextBundle, *,
                 cancel: CancelHook | None = None) -> ModelReply: ...


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def check_request(config: ProviderConfig, request: ModelRequest, bundle: ContextBundle):
    """Refuse anything that would silently change provider, model or context."""
    if request.provider != config.provider:
        raise ProviderConfigError(
            f"Request is for {request.provider}; this adapter serves {config.provider}")
    if request.model != config.model:
        raise ProviderConfigError("Request model differs from the configured model")
    if (bundle.bundle_id != request.context_bundle.id or bundle.role != request.role
            or bundle.campaign_id != request.campaign_id or bundle.task_id != request.task_id):
        raise ProviderRequestError("Context bundle does not match the request")
    if request.stream is not False:
        raise ProviderRequestError("Streaming is disabled until its capability gate passes")
