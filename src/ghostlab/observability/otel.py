"""OpenTelemetry-compatible export boundary, without an OpenTelemetry SDK.

``to_otlp`` is a pure mapping from sanitized records to the OTLP/JSON trace shape
(``resourceSpans`` / ``scopeSpans`` / ``spans``). A deployment that wants a collector
implements ``SpanExporter`` around whatever client it chooses. Nothing here opens a
connection, and no exporter ships with GhostLab.

OTLP has no null. An unknown value is omitted, never sent as 0, and
``ghostlab.usage.unknown`` names the fields that were unknown.

The ``gen_ai.*`` names follow the OpenTelemetry GenAI semantic conventions, which are
still marked as development. They haven't been checked against a live collector.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from .schema import TelemetryRecord

SCOPE_NAME = "ghostlab.observability"
_INTERNAL = 1  # SPAN_KIND_INTERNAL
_STATUS_UNSET, _STATUS_OK, _STATUS_ERROR = 0, 1, 2
DEFAULT_BATCH = 256
DEFAULT_MAX_BUFFER = 4_096


@runtime_checkable
class SpanExporter(Protocol):
    def export(self, payload: Mapping) -> None: ...


def _value(value) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attribute(key: str, value) -> dict:
    return {"key": key, "value": _value(value)}


def _nanos(timestamp: str) -> str:
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return str((delta.days * 86_400 + delta.seconds) * 10**9 + delta.microseconds * 1_000)


def _attributes(record: TelemetryRecord) -> list[dict]:
    usage = record.usage
    named = {
        "gen_ai.provider.name": record.provider, "gen_ai.request.model": record.model,
        "gen_ai.usage.input_tokens": usage.input_tokens,
        "gen_ai.usage.output_tokens": usage.output_tokens,
        "ghostlab.usage.cost_minor": usage.cost_minor,
        "ghostlab.usage.cost_currency": usage.cost_currency,
        "ghostlab.project_id": record.project_id, "ghostlab.campaign_id": record.campaign_id,
        "ghostlab.task_id": record.task_id, "ghostlab.role": record.role,
        "ghostlab.prompt_version": record.prompt_version,
        "ghostlab.skill_id": record.skill_id, "ghostlab.skill_version": record.skill_version,
        "ghostlab.workflow_version": record.workflow_version,
        "ghostlab.world_version": record.world_version,
        "ghostlab.latency_ms": record.latency_ms, "ghostlab.attempt": record.attempt,
        "ghostlab.retries": record.retries, "ghostlab.cancelled": record.cancelled,
        "ghostlab.validation_failures": record.validation_failures,
        "ghostlab.approval_id": record.approval_id,
        "ghostlab.approval_status": record.approval_status,
        "ghostlab.outcome": record.outcome, "ghostlab.error_code": record.error_code,
        "ghostlab.dropped_attributes": record.dropped_attributes or None,
    }
    named.update({f"ghostlab.attr.{key}": value for key, value in record.attributes.items()})
    result = [_attribute(key, value) for key, value in named.items() if value is not None]
    unknown = [name for name in ("input_tokens", "output_tokens", "cost_minor")
               if getattr(usage, name) is None]
    if unknown:
        result.append(_attribute("ghostlab.usage.unknown", ",".join(unknown)))
    return result


def _status(record: TelemetryRecord) -> dict:
    if record.error_code is not None or record.outcome == "error":
        return {"code": _STATUS_ERROR}
    return {"code": _STATUS_OK if record.outcome is not None else _STATUS_UNSET}


def _span(record: TelemetryRecord) -> dict:
    data = {"traceId": record.trace_id, "spanId": record.span_id, "name": record.name,
            "kind": _INTERNAL, "startTimeUnixNano": _nanos(record.started_at or record.timestamp),
            "endTimeUnixNano": _nanos(record.timestamp), "attributes": _attributes(record),
            "events": [], "links": [], "status": _status(record)}
    if record.parent_span_id is not None:
        data["parentSpanId"] = record.parent_span_id
    return data


def _span_event(record: TelemetryRecord) -> dict:
    return {"name": record.name, "timeUnixNano": _nanos(record.timestamp),
            "attributes": _attributes(record)}


def to_otlp(records: Iterable[TelemetryRecord], *, service_version: str | None = None) -> dict:
    """An OTLP/JSON ``ExportTraceServiceRequest`` for these records.

    An event becomes an event on its parent span when that span is in the batch.
    Otherwise it's a zero-length span, so nothing is lost at a batch boundary.
    """
    records = list(records)
    spans = {record.span_id: _span(record) for record in records if record.kind == "span"}
    extra = []
    for record in records:
        if record.kind == "span":
            continue
        if record.parent_span_id in spans:
            spans[record.parent_span_id]["events"].append(_span_event(record))
        else:
            extra.append(_span(record))
    scope = {"name": SCOPE_NAME, **({"version": service_version} if service_version else {})}
    return {"resourceSpans": [{
        "resource": {"attributes": [_attribute("service.name", "ghostlab")]},
        "scopeSpans": [{"scope": scope, "spans": [*spans.values(), *extra]}]}]}


class OtlpExportSink:
    """Batches records for a ``SpanExporter``.

    The buffer is bounded and an export failure drops that batch, so a dead collector
    can't grow memory or reach the business path. ``dropped`` and ``failures`` count it.
    """

    def __init__(self, exporter: SpanExporter, *, batch_size: int = DEFAULT_BATCH,
                 max_buffer: int = DEFAULT_MAX_BUFFER, service_version: str | None = None):
        if batch_size < 1 or max_buffer < batch_size:
            raise ValueError("batch_size must be positive and fit in max_buffer")
        self.exporter, self.batch_size, self.max_buffer = exporter, batch_size, max_buffer
        self.service_version = service_version
        self._buffer: list[TelemetryRecord] = []
        self.dropped = self.failures = 0

    def write(self, record: TelemetryRecord) -> None:
        if len(self._buffer) >= self.max_buffer:
            self.dropped += 1
            return
        self._buffer.append(record)
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        batch: Sequence[TelemetryRecord]
        batch, self._buffer = self._buffer, []
        if not batch:
            return
        try:
            self.exporter.export(to_otlp(batch, service_version=self.service_version))
        except Exception:  # noqa: BLE001 - telemetry never reaches the business path
            self.failures += 1
            self.dropped += len(batch)

    def close(self) -> None:
        self.flush()
