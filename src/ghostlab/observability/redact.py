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
  traces) doesn't, so the whole value is replaced.
- Long opaque tokens are replaced even if no known secret pattern matches them. Content
  digests (64 lowercase hex) and W3C ids (32 hex) are evidence and stay.
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

_SAFE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+#=-]{0,199}$")
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/=]{33,}")
_EVIDENCE_HEX = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})$")  # W3C ids and sha256 digests
_MIXED_RUN = re.compile(r"[A-Za-z0-9]{24,}")
# Common credential shapes M06's patterns don't name. Reported as a gap in M09.md.
_CREDENTIAL_SHAPES = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:gh[pousr]_[a-z0-9]{16,}|github_pat_[a-z0-9_]{16,}|"
    r"xox[abprs]-[a-z0-9-]{10,}|akia[0-9a-z]{16}|sk_(?:live|test)_[a-z0-9]{16,}|"
    r"aiza[0-9a-z_-]{30,}|ya29\.[0-9a-z_-]{16,}|sig=[^\s&]{16,}|"
    r"(?:pwd|passphrase|client_secret)\s*[:=]\s*\S{4,}|[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@|"
    r"eyj[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,})")
_SECRET_NAME = re.compile(r"(?i)(api[_-]?key|authorization|auth|bearer|cookie|credential|"
                          r"passphrase|passw|private[_-]?key|secret|session|token)")

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
        if cleaned != value or not _SAFE_TEXT.match(cleaned):
            return self.marker
        if _CREDENTIAL_SHAPES.search(cleaned):
            return self.marker
        for run in _OPAQUE_RUN.findall(cleaned):
            if not _EVIDENCE_HEX.match(run):
                return self.marker
        for run in _MIXED_RUN.findall(cleaned):  # hyphens can split a token into parts
            if not run.islower() and not run.isupper() and not run.isdigit() and any(
                    char.isdigit() for char in run):
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
