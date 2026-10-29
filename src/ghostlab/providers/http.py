"""One bounded, cancellable JSON POST with documented transient-error retry.

Retried: HTTP 429, 500, 502 and 503, plus connection-level transport errors.
Not retried: other 4xx, 504 (Meta: "don't retry unchanged") and client timeouts,
because the overall ``timeout_ms`` is the request's deadline. A model call
never dispatches tools, so a retry cannot repeat a world action; it can still
cost tokens, which is why attempts are bounded and reported.

Cancellation is observed before each attempt and during backoff. An HTTP
request already in flight is not interrupted.
"""

import json
import random
import time
from dataclasses import dataclass, field

import httpx

from ghostlab.contracts import ErrorInfo

from .base import ProviderConfig, redact

RETRYABLE_STATUS = {429: "rate_limited", 500: "provider_transient", 502: "provider_transient",
                    503: "provider_transient"}
FINAL_STATUS = {401: "unauthenticated", 403: "forbidden", 504: "deadline_exceeded"}
MAX_RESPONSE_BYTES = 4_000_000


@dataclass(frozen=True)
class Attempt:
    status_code: int | None
    error_code: str | None
    duration_ms: int


@dataclass
class CallOutcome:
    body: dict | None = None
    error: ErrorInfo | None = None
    attempts: list[Attempt] = field(default_factory=list)


@dataclass
class Runtime:
    """Injectable effects so tests never sleep or touch the network."""

    transport: httpx.BaseTransport | None = None
    sleep: object = time.sleep
    monotonic: object = time.monotonic
    jitter: object = random.random


def make_error(code, message, *, retryable=False, secrets=()) -> ErrorInfo:
    text = redact(message, *secrets)[:500] or code
    return ErrorInfo(code=code, message=text, retryable=retryable, side_effect="none", ref=None)


def _provider_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            code = error.get("code") or error.get("type") or ""
            return f"HTTP {response.status_code} {code}: {error['message']}".strip()
    except (ValueError, UnicodeDecodeError):
        pass
    return f"HTTP {response.status_code}"


def _retry_after(response: httpx.Response, cap: float) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return min(max(float(value), 0.0), cap) if value is not None else None
    except ValueError:
        return None


def _wait(seconds: float, cancel, runtime: Runtime) -> bool:
    """Sleep in short slices; return False if cancelled meanwhile."""
    remaining = seconds
    while remaining > 0:
        if cancel is not None and cancel.is_set():
            return False
        step = min(0.1, remaining)
        runtime.sleep(step)
        remaining -= step
    return not (cancel is not None and cancel.is_set())


def post_json(config: ProviderConfig, path: str, payload: dict, *, timeout_ms: int,
              cancel, runtime: Runtime) -> CallOutcome:
    secret = config.credential.reveal()
    url = config.base_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    outcome = CallOutcome()
    deadline = runtime.monotonic() + timeout_ms / 1000
    policy = config.retry

    for number in range(1, policy.max_attempts + 1):
        if cancel is not None and cancel.is_set():
            outcome.error = make_error("cancelled", "Cancelled before the request was sent")
            return outcome
        remaining = deadline - runtime.monotonic()
        if remaining <= 0:
            outcome.error = make_error("deadline_exceeded", "Deadline reached before sending")
            return outcome
        started = runtime.monotonic()
        try:
            with httpx.Client(transport=runtime.transport, timeout=httpx.Timeout(remaining),
                              follow_redirects=False) as client:
                response = client.post(url, json=payload, headers=headers)
                content = response.read()
        except httpx.TimeoutException:
            outcome.attempts.append(Attempt(None, "deadline_exceeded", _ms(runtime, started)))
            outcome.error = make_error("deadline_exceeded", f"No reply within {timeout_ms} ms")
            return outcome
        except httpx.TransportError as error:
            code, message, delay = "provider_transient", type(error).__name__, None
            outcome.attempts.append(Attempt(None, code, _ms(runtime, started)))
        else:
            duration = _ms(runtime, started)
            status = response.status_code
            if status == 200:
                outcome.attempts.append(Attempt(status, None, duration))
                if len(content) > MAX_RESPONSE_BYTES:
                    outcome.error = make_error("provider_invalid_output", "Response too large")
                    return outcome
                try:
                    body = json.loads(content)
                except (ValueError, UnicodeDecodeError):
                    outcome.error = make_error("provider_invalid_output", "Response is not JSON")
                    return outcome
                if not isinstance(body, dict):
                    outcome.error = make_error("provider_invalid_output", "Response not an object")
                    return outcome
                outcome.body = body
                return outcome
            message = _provider_message(response)
            if status in RETRYABLE_STATUS:
                code = RETRYABLE_STATUS[status]
                delay = _retry_after(response, policy.retry_after_max_s)
                outcome.attempts.append(Attempt(status, code, duration))
            else:
                code = FINAL_STATUS.get(status, "provider_permanent")
                outcome.attempts.append(Attempt(status, code, duration))
                outcome.error = make_error(code, message, secrets=(secret,))
                return outcome

        # Transient failure: back off and retry if attempts and deadline allow.
        error = make_error(code, message, retryable=True, secrets=(secret,))
        if number == policy.max_attempts:
            outcome.error = error
            return outcome
        if delay is None:
            delay = min(policy.backoff_max_s,
                        policy.backoff_base_s * 2 ** (number - 1)) + runtime.jitter()
        if runtime.monotonic() + delay >= deadline:
            outcome.error = error
            return outcome
        if not _wait(delay, cancel, runtime):
            outcome.error = make_error("cancelled", "Cancelled during retry backoff")
            return outcome
    raise AssertionError("unreachable")


def _ms(runtime: Runtime, started: float) -> int:
    return max(0, int((runtime.monotonic() - started) * 1000))
