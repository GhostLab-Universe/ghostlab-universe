"""The closed telemetry record: ``ghostlab-telemetry/1``.

Every event and span is one flat ``TelemetryRecord``. The field set is closed, so a
new field is a schema change, not a stray key. Nothing here holds a prompt, a reply,
tool arguments, a customer record or free text: a record carries identifiers,
versions, counts and outcomes, and points at evidence by id or digest.

``None`` always means unknown. A provider that reports no usage gives ``None``, never
0, and any total over unknown values stays unknown (see ``combine_usage``).
"""

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace

SCHEMA = "ghostlab-telemetry/1"
KINDS = ("event", "span")

# The names owners should use. A new name needs only to match NAME_PATTERN; add it here
# when it becomes part of the vocabulary.
EVENT_NAMES = (
    "campaign.run", "campaign.status", "task.status", "model.call", "model.attempt",
    "tool.call", "validation.failed", "retry", "cancellation", "approval.requested",
    "approval.decided", "approval.consumed", "budget.exhausted", "policy.denied",
    "run.completed", "finding.recorded", "capsule.exported", "capsule.replayed",
)
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "consumed", "denied")

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
MAX_COUNT = 10**15


def _known_count(value, label: str):
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > MAX_COUNT:
        raise ValueError(f"{label} must be a non-negative integer or None (unknown)")
    return value


@dataclass(frozen=True)
class Usage:
    """Reported usage. Each ``None`` is unknown, and unknown is not zero."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_minor: int | None = None
    cost_currency: str | None = None

    def __post_init__(self):
        _known_count(self.input_tokens, "input_tokens")
        _known_count(self.output_tokens, "output_tokens")
        _known_count(self.cost_minor, "cost_minor")
        if (self.cost_minor is None) != (self.cost_currency is None):
            raise ValueError("cost_minor and cost_currency are set together")
        if self.cost_currency is not None and not CURRENCY_PATTERN.match(self.cost_currency):
            raise ValueError("cost_currency must be a three-letter code")

    @classmethod
    def from_mapping(cls, value: Mapping | None) -> "Usage":
        """Read a contract usage dump. A missing field stays unknown."""
        value = value or {}
        return cls(*(value.get(name) for name in ("input_tokens", "output_tokens",
                                                  "cost_minor", "cost_currency")))


@dataclass(frozen=True)
class CountTotal:
    """A sum over possibly unknown values. ``total`` is None if any value was unknown."""

    known_sum: int
    known_count: int
    unknown_count: int

    @property
    def total(self) -> int | None:
        return None if self.unknown_count else self.known_sum


@dataclass(frozen=True)
class UsageTotal:
    input_tokens: CountTotal
    output_tokens: CountTotal
    cost_minor: CountTotal
    cost_currencies: tuple[str, ...]

    @property
    def cost_total(self) -> tuple[int, str] | None:
        """A cost, only when every value is known and in one currency."""
        if self.cost_minor.total is None or len(self.cost_currencies) != 1:
            return None
        return self.cost_minor.total, self.cost_currencies[0]


def _count_total(values: Iterable[int | None]) -> CountTotal:
    values = list(values)
    known = [value for value in values if value is not None]
    return CountTotal(sum(known), len(known), len(values) - len(known))


def combine_usage(items: Iterable[Usage]) -> UsageTotal:
    """Add usage without turning unknown into zero."""
    items = list(items)
    return UsageTotal(_count_total(item.input_tokens for item in items),
                      _count_total(item.output_tokens for item in items),
                      _count_total(item.cost_minor for item in items),
                      tuple(sorted({item.cost_currency for item in items
                                    if item.cost_currency is not None})))


@dataclass(frozen=True)
class Versions:
    """What produced the work. A field is None when it doesn't apply or isn't known."""

    role: str | None = None
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None
    workflow_version: str | None = None
    world_version: str | None = None


@dataclass(frozen=True)
class TraceContext:
    """Where a record belongs. Passed explicitly, so it survives threads and restarts.

    ``span_id`` is the current span: records written with this context take it as their
    parent. Use the campaign's own ``trace_id`` so a restarted campaign keeps its trace.
    """

    trace_id: str
    project_id: str | None = None
    campaign_id: str | None = None
    task_id: str | None = None
    span_id: str | None = None
    versions: Versions = field(default_factory=Versions)

    def __post_init__(self):
        if not TRACE_ID_PATTERN.match(self.trace_id):
            raise ValueError("trace_id must be 32 lowercase hex characters")
        if self.span_id is not None and not SPAN_ID_PATTERN.match(self.span_id):
            raise ValueError("span_id must be 16 lowercase hex characters")

    def with_task(self, task_id: str | None, **versions) -> "TraceContext":
        """The same trace for one task, optionally with more versions (role, model...)."""
        return replace(self, task_id=task_id, versions=replace(self.versions, **versions))


@dataclass(frozen=True)
class TelemetryRecord:
    """One sanitized event or span. Build these with ``Telemetry``, which redacts."""

    schema: str
    kind: str
    name: str
    timestamp: str
    started_at: str | None
    trace_id: str
    span_id: str
    parent_span_id: str | None
    project_id: str | None
    campaign_id: str | None
    task_id: str | None
    role: str | None
    provider: str | None
    model: str | None
    prompt_version: str | None
    skill_id: str | None
    skill_version: str | None
    workflow_version: str | None
    world_version: str | None
    latency_ms: int | None
    usage: Usage
    attempt: int | None
    retries: int | None
    cancelled: bool | None
    validation_failures: int | None
    approval_id: str | None
    approval_status: str | None
    outcome: str | None
    error_code: str | None
    attributes: Mapping
    dropped_attributes: int

    def to_json(self) -> dict:
        data = asdict(self)
        data["attributes"] = dict(self.attributes)
        return data


RECORD_FIELDS = tuple(item.name for item in fields(TelemetryRecord))
_OPTIONAL_TEXT = ("project_id", "campaign_id", "task_id", "role", "provider", "model",
                  "prompt_version", "skill_id", "skill_version", "workflow_version",
                  "world_version", "outcome", "error_code", "approval_id")
_OPTIONAL_COUNTS = ("latency_ms", "attempt", "retries", "validation_failures")
_USAGE_FIELDS = tuple(item.name for item in fields(Usage))


def _scalar(value) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (bool, int, str))


def validate_record(data: Mapping) -> list[str]:
    """Problems with a stored record, or an empty list. Stdlib only, no coercion."""
    if not isinstance(data, Mapping):
        return ["record is not an object"]
    problems = []
    if missing := set(RECORD_FIELDS) - set(data):
        problems.append(f"missing fields: {sorted(missing)}")
    if extra := set(data) - set(RECORD_FIELDS):
        problems.append(f"unknown fields: {sorted(map(str, extra))}")
    if problems:
        return problems
    if data["schema"] != SCHEMA:
        problems.append("unknown schema")
    if data["kind"] not in KINDS:
        problems.append("unknown kind")
    if not isinstance(data["name"], str) or not NAME_PATTERN.match(data["name"]):
        problems.append("invalid name")
    for name in ("timestamp", "started_at"):
        value = data[name]
        if not (value is None and name == "started_at") and (
                not isinstance(value, str) or not TIMESTAMP_PATTERN.match(value)):
            problems.append(f"invalid {name}")
    if (data["kind"] == "span") != (data["started_at"] is not None):
        problems.append("started_at belongs to spans, and every span has one")
    if not isinstance(data["trace_id"], str) or not TRACE_ID_PATTERN.match(data["trace_id"]):
        problems.append("invalid trace_id")
    if not isinstance(data["span_id"], str) or not SPAN_ID_PATTERN.match(data["span_id"]):
        problems.append("invalid span_id")
    parent = data["parent_span_id"]
    if parent is not None and (not isinstance(parent, str) or not SPAN_ID_PATTERN.match(parent)):
        problems.append("invalid parent_span_id")
    for name in _OPTIONAL_TEXT:
        if data[name] is not None and not isinstance(data[name], str):
            problems.append(f"{name} must be text or null")
    for name in _OPTIONAL_COUNTS:
        value = data[name]
        if value is not None and (type(value) is not int or value < 0):
            problems.append(f"{name} must be a non-negative integer or null")
    if data["cancelled"] is not None and type(data["cancelled"]) is not bool:
        problems.append("cancelled must be a boolean or null")
    if data["approval_status"] is not None and data["approval_status"] not in APPROVAL_STATUSES:
        problems.append("unknown approval_status")
    usage = data["usage"]
    if not isinstance(usage, Mapping) or set(usage) != set(_USAGE_FIELDS):
        problems.append("usage must have exactly its four fields")
    else:
        try:
            Usage.from_mapping(usage)
        except ValueError as error:
            problems.append(f"invalid usage: {error}")
    attributes = data["attributes"]
    if not isinstance(attributes, Mapping) or not all(
            isinstance(key, str) and _scalar(value) for key, value in attributes.items()):
        problems.append("attributes must be a flat object of scalars")
    if type(data["dropped_attributes"]) is not int or data["dropped_attributes"] < 0:
        problems.append("dropped_attributes must be a non-negative integer")
    return problems
