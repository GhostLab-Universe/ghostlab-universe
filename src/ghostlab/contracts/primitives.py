"""Strict base model and constrained scalar types shared by every contract.

Statuses are ``Literal`` strings and timestamps are RFC 3339 UTC strings so the
same payload validates identically from Python objects and from JSON.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field,
                      StringConstraints, model_validator)


def _read_only(self, *args, **kwargs):
    raise TypeError(f"{type(self).__name__} is immutable")


class FrozenList(list):
    """A list that rejects mutation; still a list for schemas and serialization."""

    __setitem__ = __delitem__ = __iadd__ = __imul__ = _read_only
    append = extend = insert = pop = remove = clear = sort = reverse = _read_only

    def __reduce__(self):
        return (type(self), (list(self),))


class FrozenDict(dict):
    """A dict that rejects mutation; still a dict for schemas and serialization."""

    __setitem__ = __delitem__ = __ior__ = _read_only
    clear = pop = popitem = setdefault = update = _read_only

    def __reduce__(self):
        return (type(self), (dict(self),))


def freeze(value):
    """Deep-freeze plain lists/dicts; nested models are already frozen."""
    if isinstance(value, BaseModel):
        return value
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(freeze(item) for item in value)
    return value


class StrictModel(BaseModel):
    """No coercion, no unknown fields, immutable after validation (collections too).

    ``model_construct`` and ``model_copy(update=...)`` skip validation and so
    skip freezing; contract consumers must not use them on untrusted data.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _freeze_collections(self):
        for name in type(self).model_fields:
            object.__setattr__(self, name, freeze(getattr(self, name)))
        return self


def _exact_type(expected: type):
    def check(value):
        # Literal[1] alone accepts True and 1.0, and Literal[False] accepts 0.
        if type(value) is not expected:
            raise ValueError(f"must be a JSON {expected.__name__}, not {type(value).__name__}")
        return value
    return BeforeValidator(check)


SchemaVersion = Annotated[Literal[1], _exact_type(int)]
FalseFlag = Annotated[Literal[False], _exact_type(bool)]


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
