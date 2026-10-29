"""Strict base model and constrained scalar types shared by every contract.

Statuses are ``Literal`` strings and timestamps are RFC 3339 UTC strings so the
same payload validates identically from Python objects and from JSON.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


class StrictModel(BaseModel):
    """No coercion, no unknown fields, immutable after validation."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


def _utc_timestamp(value: str) -> str:
    datetime.fromisoformat(value.replace("Z", "+00:00"))  # rejects impossible dates
    return value


Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
ModelId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")]
SchemaName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]  # W3C trace-context
SpanId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
MediaType = Annotated[str, StringConstraints(pattern=r"^[a-z]+/[a-z0-9.+-]{1,100}$")]
UtcTimestamp = Annotated[
    str,
    # ASCII digits only: \d means Unicode digits in some regex engines.
    StringConstraints(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"),
    AfterValidator(_utc_timestamp),
]

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=8_000)]

# Integer minor currency units (paise for INR). Strict mode rejects bool/float/str.
MinorUnits = Annotated[int, Field(ge=0, le=10**15)]
PositiveMinorUnits = Annotated[int, Field(ge=1, le=10**15)]
Count = Annotated[int, Field(ge=0, le=10**12)]
PositiveCount = Annotated[int, Field(ge=1, le=10**12)]

AgentRole = Literal["supervisor", "worldsmith", "explorer", "analyst", "target", "verifier"]
ModelRole = Literal["worldsmith", "explorer", "analyst", "target"]
ToolName = Literal["get_order", "issue_refund", "get_refund_status"]
WorldVersion = Literal["refund-world/1"]


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def require_unique(values, label: str):
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must not contain duplicates")
