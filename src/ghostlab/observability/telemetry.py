"""The emitter owners inject: ``Telemetry``.

Everything is injectable (sink, clock, timer, id generator, redactor), so a test can pin
a trace exactly and a component can default to ``NULL_TELEMETRY`` without new wiring.

Telemetry never changes a business result. A failing sink, or a record that can't be
sanitized, is counted and dropped; nothing raises into the caller, and a bad record is
never written unredacted. Pass ``strict=True`` in tests so instrumentation bugs surface.
"""

import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from .redact import Sanitizer, TextRedactor
from .schema import (APPROVAL_STATUSES, NAME_PATTERN, SCHEMA, SPAN_ID_PATTERN, TelemetryRecord,
                     TraceContext, Usage, Versions)
from .sinks import NullSink, TelemetrySink


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_span_id() -> str:
    return secrets.token_hex(8)


@dataclass
class Span:
    """An open span. Set results on it; the record is written when it closes."""

    context: TraceContext  # pass this to child spans and events
    outcome: str | None = None
    error_code: str | None = None
    usage: Usage = field(default_factory=Usage)
    attempt: int | None = None
    retries: int | None = None
    cancelled: bool | None = None
    validation_failures: int | None = None
    approval_id: str | None = None
    approval_status: str | None = None
    attributes: dict = field(default_factory=dict)


class _SpanScope:
    def __init__(self, telemetry, name, span, parent_id, started_at, started):
        self.telemetry, self.name, self.span = telemetry, name, span
        self.parent_id, self.started_at, self.started = parent_id, started_at, started

    def __enter__(self) -> Span:
        return self.span

    def __exit__(self, kind, error, traceback):
        span, telemetry = self.span, self.telemetry
        if kind is not None:
            # The class name only: exception messages can hold request data.
            span.outcome = span.outcome or "error"
            span.attributes.setdefault("error_class", kind.__name__)
        latency = max(0, round((telemetry.monotonic() - self.started) * 1000))
        telemetry._write("span", self.name, span.context.span_id, self.parent_id, span.context,
                         started_at=self.started_at, latency_ms=latency, outcome=span.outcome,
                         error_code=span.error_code, usage=span.usage, attempt=span.attempt,
                         retries=span.retries, cancelled=span.cancelled,
                         validation_failures=span.validation_failures,
                         approval_id=span.approval_id, approval_status=span.approval_status,
                         attributes=span.attributes)
        return False


class Telemetry:
    def __init__(self, sink: TelemetrySink | None = None, *,
                 clock: Callable[[], datetime] = _utc_now,
                 monotonic: Callable[[], float] = time.monotonic,
                 new_span_id: Callable[[], str] = _new_span_id,
                 redactor: TextRedactor | None = None,
                 extra_attributes: Iterable[str] = (), strict: bool = False):
        self.sink = sink or NullSink()
        self.clock, self.monotonic, self.new_span_id = clock, monotonic, new_span_id
        self._redactor, self._extra = redactor, tuple(extra_attributes)
        self._sanitizer: Sanitizer | None = None
        self.strict = strict
        self.written = self.rejected = self.sink_failures = 0
        if redactor is not None:
            self._sanitizer = Sanitizer(redactor, self._extra)

    @property
    def sanitizer(self) -> Sanitizer:
        if self._sanitizer is None:  # resolved late: this reads the environment's secrets
            self._sanitizer = Sanitizer(None, self._extra)
        return self._sanitizer

    def event(self, name: str, context: TraceContext, *, outcome: str | None = None,
              error_code: str | None = None, usage: Usage | None = None,
              latency_ms: int | None = None, attempt: int | None = None,
              retries: int | None = None, cancelled: bool | None = None,
              validation_failures: int | None = None, approval_id: str | None = None,
              approval_status: str | None = None, attributes: Mapping | None = None) -> None:
        """Write one point-in-time record under the context's current span."""
        self._write("event", name, None, context.span_id, context, started_at=None,
                    latency_ms=latency_ms, outcome=outcome, error_code=error_code,
                    usage=usage or Usage(), attempt=attempt, retries=retries, cancelled=cancelled,
                    validation_failures=validation_failures, approval_id=approval_id,
                    approval_status=approval_status, attributes=attributes)

    def span(self, name: str, context: TraceContext, **versions) -> _SpanScope:
        """A timed scope: ``with telemetry.span("model.call", ctx, role="explorer") as s:``."""
        span_id = self.new_span_id()
        child = replace(context, span_id=span_id,
                        versions=replace(context.versions, **versions))
        return _SpanScope(self, name, Span(child), context.span_id, _timestamp(self.clock()),
                          self.monotonic())

    def close(self) -> None:
        try:
            self.sink.close()
        except Exception:  # noqa: BLE001
            self.sink_failures += 1

    def _write(self, kind, name, span_id, parent_id, context, *, started_at, latency_ms, outcome,
               error_code, usage, attempt, retries, cancelled, validation_failures, approval_id,
               approval_status, attributes):
        try:
            record = self._record(kind, name, span_id or self.new_span_id(), parent_id, context,
                                  started_at, latency_ms, outcome, error_code, usage, attempt,
                                  retries, cancelled, validation_failures, approval_id,
                                  approval_status, attributes)
        except Exception:
            self.rejected += 1
            if self.strict:
                raise
            return
        try:
            self.sink.write(record)
            self.written += 1
        except Exception:
            self.sink_failures += 1
            if self.strict:
                raise

    def _record(self, kind, name, span_id, parent_id, context: TraceContext, started_at,
                latency_ms, outcome, error_code, usage: Usage, attempt, retries, cancelled,
                validation_failures, approval_id, approval_status,
                attributes: Mapping[str, Any] | None) -> TelemetryRecord:
        if not isinstance(name, str) or not NAME_PATTERN.match(name):
            raise ValueError("telemetry name must be a dotted lowercase identifier")
        if not SPAN_ID_PATTERN.match(span_id):
            raise ValueError("span ids must be 16 lowercase hex characters")
        if approval_status is not None and approval_status not in APPROVAL_STATUSES:
            raise ValueError("unknown approval status")
        if not isinstance(usage, Usage):
            raise ValueError("usage must be a Usage; None means unknown")
        clean = self.sanitizer
        kept, dropped = clean.attribute_map(attributes)
        versions: Versions = context.versions
        return TelemetryRecord(
            schema=SCHEMA, kind=kind, name=name, timestamp=_timestamp(self.clock()),
            started_at=started_at, trace_id=context.trace_id, span_id=span_id,
            parent_span_id=parent_id, project_id=clean.text(context.project_id),
            campaign_id=clean.text(context.campaign_id), task_id=clean.text(context.task_id),
            role=clean.text(versions.role), provider=clean.text(versions.provider),
            model=clean.text(versions.model), prompt_version=clean.text(versions.prompt_version),
            skill_id=clean.text(versions.skill_id), skill_version=clean.text(versions.skill_version),
            workflow_version=clean.text(versions.workflow_version),
            world_version=clean.text(versions.world_version), latency_ms=latency_ms, usage=usage,
            attempt=attempt, retries=retries, cancelled=cancelled,
            validation_failures=validation_failures, approval_id=clean.text(approval_id),
            approval_status=approval_status, outcome=clean.text(outcome),
            error_code=clean.text(error_code), attributes=kept, dropped_attributes=dropped)


NULL_TELEMETRY = Telemetry(NullSink(), redactor=None)
