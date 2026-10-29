"""One bounded, cancellable JSON POST with documented transient-error retry.

Retried: HTTP 429, 500, 502 and 503, plus connection-level transport errors.
Not retried: other 4xx, 504 (Meta: "don't retry unchanged") and client timeouts,
because the overall ``timeout_ms`` is the request's deadline. A model call
never dispatches tools, so a retry cannot repeat a world action; it can still
cost tokens, which is why attempts are bounded and reported.

Cancellation is observed before each attempt and during backoff. An HTTP
request already in flight is not interrupted.

Every outbound attempt, retries included, first asks ``Runtime.attempt_gate``
(when set) for permission, so a caller can cap or reserve paid attempts. Bodies
are streamed with an incremental byte cap (success and error responses alike)
and the request's absolute deadline is checked while reading, not only between
attempts.
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
MAX_ERROR_BODY_BYTES = 65_536  # error bodies are only read for their message


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
    # Called before every outbound HTTP attempt; returning False stops before sending.
    attempt_gate: object = None


class _ReadAborted(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def make_error(code, message, *, retryable=False, secrets=()) -> ErrorInfo:
    text = redact(message, *secrets)[:500] or code
    return ErrorInfo(code=code, message=text, retryable=retryable, side_effect="none", ref=None)


def _provider_message(status: int, content: bytes) -> str:
    try:
        body = json.loads(content)
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            code = error.get("code") or error.get("type") or ""
            return f"HTTP {status} {code}: {error['message']}".strip()
    except (ValueError, UnicodeDecodeError):
        pass
    return f"HTTP {status}"


def _read_bounded(response: httpx.Response, limit: int, *, truncate: bool, deadline: float,
                  runtime: Runtime) -> bytes:
    """Read at most ``limit`` bytes, checking the absolute deadline between chunks.

    Over the limit, a success body is rejected and an error body is cut short;
    either way the rest of the body is never downloaded.
    """
    declared = response.headers.get("content-length")
    if not truncate and declared is not None and declared.isdigit() and int(declared) > limit:
        raise _ReadAborted("provider_invalid_output", "Response too large")
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        if runtime.monotonic() >= deadline:
            raise _ReadAborted("deadline_exceeded", "Deadline reached while reading the reply")
        if size + len(chunk) > limit:
            if truncate:
                chunks.append(chunk[:limit - size])
                break
            raise _ReadAborted("provider_invalid_output", "Response too large")
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


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
        if runtime.attempt_gate is not None and not runtime.attempt_gate():
            outcome.error = make_error("budget_exhausted",
                                       "HTTP attempt cap reached; the request was not sent")
            return outcome
        started = runtime.monotonic()
        status = None
        try:
            with httpx.Client(transport=runtime.transport, timeout=httpx.Timeout(remaining),
                              follow_redirects=False) as client:
                with client.stream("POST", url, json=payload, headers=headers) as response:
                    status = response.status_code
                    delay = (_retry_after(response, policy.retry_after_max_s)
                             if status in RETRYABLE_STATUS else None)
                    content = _read_bounded(
                        response, MAX_RESPONSE_BYTES if status == 200 else MAX_ERROR_BODY_BYTES,
                        truncate=status != 200, deadline=deadline, runtime=runtime)
        except _ReadAborted as abort:
            outcome.attempts.append(Attempt(status, abort.code, _ms(runtime, started)))
            outcome.error = make_error(abort.code, str(abort))
            return outcome
        except httpx.TimeoutException:
            outcome.attempts.append(Attempt(None, "deadline_exceeded", _ms(runtime, started)))
            outcome.error = make_error("deadline_exceeded", f"No reply within {timeout_ms} ms")
            return outcome
        except httpx.TransportError as error:
            code, message, delay = "provider_transient", type(error).__name__, None
            outcome.attempts.append(Attempt(None, code, _ms(runtime, started)))
        else:
            duration = _ms(runtime, started)
            if status == 200:
                outcome.attempts.append(Attempt(status, None, duration))
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
            message = _provider_message(status, content)
            if status in RETRYABLE_STATUS:
                code = RETRYABLE_STATUS[status]
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
