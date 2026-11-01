"""Outbound content handling for requests that would leave the process.

Every adapter call passes through ``post_json`` (``ghostlab.providers.http``),
so this guard covers every caller -- whatever session, campaign or script sits
above it -- by construction. ``httpx.MockTransport`` keeps requests in process
(the live-inference gate in ``ghostlab.orchestration.inference`` treats it the
same way) and is exempt; any other transport may reach the network and needs
an explicit ``OutboundPolicy``.

The policy carries the operator's retention decision
(``allow_server_retention``), declared on the wire where the provider keeps
server-side state (Meta ``store``); a cap on the serialized request body
(``max_body_bytes``), measured on the exact bytes that would be sent; and a
bound on concurrent live attempts (``max_inflight``) shared by every call
derived from one policy object. Copies carry their own slots, so callers share
the policy object to share the bound. The N+1st concurrent attempt is refused
before anything is sent.

The content check scans the serialized body for the configured credential and
for secret-shaped text. It reads bytes, not trust labels: a ``trusted`` label
never exempts content, and clean ``untrusted`` content is sent.

Refusals raise ``OutboundRefused`` with a fixed reason before the first byte
is sent. The API maps each reason to a non-5xx status
(``ghostlab.api.errors``).
"""

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

from ghostlab.security.redaction import SECRET_TEXT_PATTERNS

from .base import ProviderConfigError

RETENTION_NOT_AUTHORIZED = "retention_not_authorized"
PROHIBITED_CONTENT = "prohibited_content"
PAYLOAD_TOO_LARGE = "payload_too_large"
CONCURRENCY_EXHAUSTED = "concurrency_exhausted"

_MESSAGES = {
    RETENTION_NOT_AUTHORIZED: "Outbound request refused: server-side retention is not authorized",
    PROHIBITED_CONTENT: "Outbound request refused: the body holds prohibited content",
    PAYLOAD_TOO_LARGE: "Outbound request refused: the body exceeds its size bound",
    CONCURRENCY_EXHAUSTED: "Outbound request refused: no live inference slot is free",
}

# Explorer bundles stay in the tens of kilobytes (token_budget 8000); schemas
# and tool definitions in single kilobytes. One megabyte is thirty times that
# with room for growth, and still a quarter of the inbound response cap.
DEFAULT_MAX_BODY_BYTES = 1_000_000
# One live attempt per active campaign worker (M07's default); the N+1st waits
# nowhere and is refused, so a stuck provider cannot pile up threads.
DEFAULT_MAX_INFLIGHT = 4


class OutboundRefused(Exception):
    """A request the outbound policy refused. Raised before anything is sent."""

    def __init__(self, reason: str):
        if reason not in _MESSAGES:
            raise ValueError(f"Unknown outbound refusal {reason!r}")
        super().__init__(_MESSAGES[reason])
        self.reason = reason


@dataclass(frozen=True)
class OutboundPolicy:
    """Explicit operator posture for requests that may leave the process."""

    allow_server_retention: bool
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_inflight: int = DEFAULT_MAX_INFLIGHT
    _slots: threading.BoundedSemaphore = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if type(self.allow_server_retention) is not bool:
            raise ProviderConfigError("allow_server_retention must be an explicit bool")
        for name in ("max_body_bytes", "max_inflight"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ProviderConfigError(f"{name} must be a positive integer")
        object.__setattr__(self, "_slots", threading.BoundedSemaphore(self.max_inflight))

    def check(self, body: bytes, *, secret: str) -> None:
        """Refuse an oversized or prohibited body before it is sent."""
        if len(body) > self.max_body_bytes:
            raise OutboundRefused(PAYLOAD_TOO_LARGE)
        if secret and secret.encode("utf-8") in body:
            raise OutboundRefused(PROHIBITED_CONTENT)
        text = body.decode("utf-8", errors="replace")
        if any(pattern.search(text) for pattern in SECRET_TEXT_PATTERNS):
            raise OutboundRefused(PROHIBITED_CONTENT)

    @contextmanager
    def slot(self):
        """One live attempt's concurrency slot; the N+1st is refused outright."""
        if not self._slots.acquire(blocking=False):
            raise OutboundRefused(CONCURRENCY_EXHAUSTED)
        try:
            yield
        finally:
            self._slots.release()
