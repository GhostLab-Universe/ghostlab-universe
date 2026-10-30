"""M09 telemetry: ``ghostlab-telemetry/1``. Standard library only.

Owners inject a ``Telemetry`` and write events and spans against a ``TraceContext``.
Nothing is sent anywhere by default. See docs/modules/M09.md.
"""

from .otel import OtlpExportSink, SpanExporter, to_otlp
from .redact import ATTRIBUTE_ALLOWLIST, Sanitizer, TextRedactor, default_redactor, find_secrets
from .schema import (APPROVAL_STATUSES, EVENT_NAMES, RECORD_FIELDS, SCHEMA, CountTotal,
                     TelemetryRecord, TraceContext, Usage, UsageTotal, Versions, combine_usage,
                     validate_record)
from .sinks import (FanOutSink, FileSink, InMemorySink, NullSink, TelemetrySink, prune_expired,
                    read_records, record_lines, sink_files)
from .telemetry import NULL_TELEMETRY, Span, Telemetry

__all__ = ["APPROVAL_STATUSES", "ATTRIBUTE_ALLOWLIST", "EVENT_NAMES", "NULL_TELEMETRY",
           "RECORD_FIELDS", "SCHEMA", "CountTotal", "FanOutSink", "FileSink", "InMemorySink",
           "NullSink", "OtlpExportSink", "Sanitizer", "Span", "SpanExporter", "Telemetry",
           "TelemetryRecord", "TelemetrySink", "TextRedactor", "TraceContext", "Usage",
           "UsageTotal", "Versions", "combine_usage", "default_redactor", "find_secrets",
           "prune_expired", "read_records", "record_lines", "sink_files", "to_otlp",
           "validate_record"]
