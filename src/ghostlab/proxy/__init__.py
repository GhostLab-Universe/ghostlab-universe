"""Loopback boundary proxy with deterministic fault injection (Track E1)."""

from .proxy import (
    MUTATING_PATHS,
    PERMISSION_REVOKED,
    TIMEOUT_AFTER_COMMIT,
    BoundaryProxy,
)

__all__ = [
    "MUTATING_PATHS",
    "PERMISSION_REVOKED",
    "TIMEOUT_AFTER_COMMIT",
    "BoundaryProxy",
]
