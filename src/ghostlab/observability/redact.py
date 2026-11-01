"""Deny-by-default sanitizing for telemetry, on top of M06's ``Redactor``.

The redaction rules live in ``ghostlab.security.redaction`` and are not copied here.
That module is standard library only, but importing it through the ``ghostlab.security``
package also imports the Pydantic contracts. So the default redactor loads the same file
directly, which keeps this package dependency-free. If the real module is already
imported, it is used as is. Anything with ``text`` and ``value`` methods can be injected.

Telemetry adds what a text scrubber can't do: it decides what may be recorded at all.

- Only allowlisted attribute names survive, and only with scalar values. Everything else
  is dropped and counted, never recorded.
- Text must look like an identifier. Free text (prompts, messages, summaries, stack
  traces) doesn't, so the whole value is replaced. So does an email address, or anything
  written as ``secret-name=value``.
- Long opaque tokens are replaced even if no known secret pattern matches them, and so are
  tokens split into parts by a separator. A content digest (64 lowercase hex) is evidence
  and stays, which also means a 64-hex secret stays: the two can't be told apart.
- A trace id has its own validated field, so 32 hex in text is treated as a key. A token of
  separated words with no digits, or a short one, looks like a name and stays.
"""

import importlib.util
import math
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

_REDACTION_MODULE = "ghostlab.security.redaction"
_PRIVATE_NAME = "ghostlab._observability_redaction"

_SAFE_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+#=-]{0,199}")
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/=]{33,}")
_EVIDENCE_HEX = re.compile(r"[0-9a-f]{64}")  # sha256 digests
_LONG_RUN = re.compile(r"[A-Za-z0-9]{24,}")
_SEGMENT = re.compile(r"[A-Za-z0-9]{4,}")
# Common credential shapes M06's patterns don't name. Reported as a gap in M09.md.
_CREDENTIAL_SHAPES = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:gh[pousr]_[a-z0-9]{16,}|github_pat_[a-z0-9_]{16,}|"
    r"xox[abprs]-[a-z0-9-]{10,}|akia[0-9a-z]{16}|sk_(?:live|test)_[a-z0-9]{16,}|"
    r"aiza[0-9a-z_-]{30,}|ya29\.[0-9a-z_-]{16,}|sig=[^\s&]{16,}|"
    r"(?:pwd|passphrase|client_secret)\s*[:=]\s*\S{4,}|[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@|"
    r"eyj[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,})")
_SECRET_WORDS = (r"api[_-]?key|authorization|auth|bearer|cookie|credential|passphrase|passw|"
                 r"private[_-]?key|secret|session|token")
_SECRET_NAME = re.compile(rf"(?i)({_SECRET_WORDS})")
# Only for one value that should be an identifier. A whole dump, which ``find_secrets`` reads,
# can hold a synthetic address or a key named "session" with no secret in it.
_NOT_AN_IDENTIFIER = re.compile(rf"(?i)(?:{_SECRET_WORDS}|basic)[a-z0-9_-]*[:=]|"
                                r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")

# Numeric counters whose name contains a denied word are fine; everything else is not.
_COUNTER_NAMES = frozenset({"input_tokens", "output_tokens", "total_tokens"})

ATTRIBUTE_ALLOWLIST = frozenset({
    "adapter", "budget_resource", "capsule_kind", "decision_code", "digest", "error_class",
    "event_count", "execution_mode", "fault_id", "finding_count", "guarded", "held_by", "http_status",
    "invariant", "iteration", "model_calls", "node", "reproduced", "request_id", "run_id",
    "run_kind", "run_status", "sequence", "side_effect", "tool_calls", "tool_name",
    "transport", "verdict", "world_event_id",
})


class TextRedactor(Protocol):
    """What ``ghostlab.security.redaction.Redactor`` provides."""

    def text(self, value: str) -> str: ...

    def value(self, value): ...


def _redaction_module():
    module = sys.modules.get(_REDACTION_MODULE)
    if module is not None:
        return module
    module = sys.modules.get(_PRIVATE_NAME)
    if module is not None:
        return module
    path = Path(__file__).resolve().parent.parent / "security" / "redaction.py"
    spec = importlib.util.spec_from_file_location(_PRIVATE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PRIVATE_NAME] = module
    spec.loader.exec_module(module)
    return module


def redacted_marker() -> str:
    return _redaction_module().REDACTED


def default_redactor() -> TextRedactor:
    """M06's redactor, armed with the secrets currently in the environment."""
    return _redaction_module().Redactor.from_env()


def check_attribute_names(names: Iterable[str]) -> frozenset[str]:
    """Attribute names an owner wants to add. Secret-looking names are refused."""
    names = frozenset(names)
    for name in names:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ValueError(f"invalid attribute name {name!r}")
        if name not in _COUNTER_NAMES and _SECRET_NAME.search(name):
            raise ValueError(f"attribute name {name!r} looks like a secret")
    return names


def _looks_opaque(text: str) -> bool:
    """A long run, or a token that a separator split into parts that hold no word."""
    if any(not _EVIDENCE_HEX.fullmatch(run)
           for run in (*_OPAQUE_RUN.findall(text), *_LONG_RUN.findall(text))):
        return True
    parts = [part for part in _SEGMENT.findall(text) if not _EVIDENCE_HEX.fullmatch(part)
             and any(char.isdigit() for char in part) and any(char.isalpha() for char in part)]
    return len(parts) >= 2 and sum(map(len, parts)) >= 16


class Sanitizer:
    def __init__(self, redactor: TextRedactor | None = None,
                 extra_attributes: Iterable[str] = ()):
        self.redactor = redactor or default_redactor()
        self.marker = redacted_marker()
        self.attributes = ATTRIBUTE_ALLOWLIST | check_attribute_names(extra_attributes)

    def text(self, value) -> str | None:
        """An identifier-shaped string, or the redaction marker. None stays None."""
        if value is None:
            return None
        if not isinstance(value, str):
            return self.marker
        cleaned = self.redactor.text(value)
        if cleaned != value or not _SAFE_TEXT.fullmatch(cleaned):
            return self.marker
        if (_CREDENTIAL_SHAPES.search(cleaned) or _NOT_AN_IDENTIFIER.search(cleaned)
                or _looks_opaque(cleaned)):
            return self.marker
        return cleaned

    def attribute_map(self, attributes: Mapping | None) -> tuple[dict, int]:
        """Allowed attributes, and how many were dropped."""
        kept, dropped = {}, 0
        for key, value in (attributes or {}).items():
            if key not in self.attributes:
                dropped += 1
            elif value is None or isinstance(value, (bool, int)):
                kept[key] = value
            elif isinstance(value, float) and math.isfinite(value):
                kept[key] = value
            elif isinstance(value, str):
                kept[key] = self.text(value)
            else:
                dropped += 1
        return kept, dropped


def find_secrets(text: str, literals: Iterable[str] = ()) -> list[str]:
    """Labels of what a telemetry dump must never contain. For audits and tests.

    Returns labels, not matches, so a report built from this can't itself leak.
    """
    module = _redaction_module()
    found = []
    for index, literal in enumerate(literals):
        if literal and literal in text:
            found.append(f"literal[{index}]")
    if module.Redactor().text(text) != text:
        found.append("secret_pattern")
    if _CREDENTIAL_SHAPES.search(text):
        found.append("credential_shape")
    return found
