"""Secret redaction for logs, errors, traces and exported evidence.

Content digests (64 lowercase hex) are evidence and are deliberately left intact.
"""

import logging
import os
import re
from collections.abc import Iterable, Mapping

REDACTED = "[REDACTED]"

SECRET_ENV_NAMES: tuple[str, ...] = ("MODEL_API_KEY", "BASETEN_API_KEY", "LANGSMITH_API_KEY")

# Whole key names (or a trailing _segment), so usage fields like input_tokens survive.
_SECRET_KEY = re.compile(r"(?i)(^|[_-])(api[_-]?key|secret[_-]?key|authorization|token|secret|"
                         r"password|passwd|credentials?)$")
_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|token)"
               r"(\s*[:=]\s*)(\"?)[^\s\"',;]{4,}"),
)


class Redactor:
    def __init__(self, literals: Iterable[str] = ()):
        # Longest first so overlapping secrets are fully removed.
        self._literals = tuple(sorted({s for s in literals if len(s) >= 8}, key=len,
                                      reverse=True))

    @classmethod
    def from_env(cls, names: Iterable[str] = SECRET_ENV_NAMES,
                 environ: Mapping[str, str] | None = None) -> "Redactor":
        environ = os.environ if environ is None else environ
        return cls(environ.get(name, "") for name in names)

    def text(self, value: str) -> str:
        for literal in self._literals:
            value = value.replace(literal, REDACTED)
        for pattern in _PATTERNS[:3]:
            value = pattern.sub(REDACTED, value)
        return _PATTERNS[3].sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}",
                                value)

    def value(self, value):
        """Recursively redact strings; values under secret-looking keys are dropped."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {key: REDACTED if isinstance(key, str) and _SECRET_KEY.search(key)
                    else self.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the fully formatted message before handlers see it."""

    def __init__(self, redactor: Redactor):
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor.text(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = self.redactor.text(logging.Formatter().formatException(
                record.exc_info))
            record.exc_info = None
        return True
