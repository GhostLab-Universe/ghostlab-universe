"""Versioned ExecutionBackend interface (frozen surface, execution-backend/1).

This module defines the controller/job contract and nothing else: error
types, the controller-assigned :class:`JobSpec`, lifecycle states, and the
abstract :class:`ExecutionBackend`. It performs no execution and imports no
adapter, world, or backend implementation code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

BACKEND_VERSION = "execution-backend/1"

# Lifecycle states shared by every backend implementation.
STATE_PREPARED = "prepared"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_TIMED_OUT = "timed_out"

STATES = (
    STATE_PREPARED,
    STATE_STARTING,
    STATE_RUNNING,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_CANCELLED,
    STATE_TIMED_OUT,
)

TERMINAL_STATES = frozenset({
    STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED, STATE_TIMED_OUT,
})

_NON_TERMINAL_STATES = frozenset(
    {STATE_PREPARED, STATE_STARTING, STATE_RUNNING})

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ADAPTER_REF_PATTERN = re.compile(
    r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*:[A-Za-z_][A-Za-z0-9_]*$")

# Sanity ceilings for controller-assigned limits (backends may be stricter).
_MAX_WALL_CLOCK_MS = 24 * 60 * 60 * 1000
_MAX_OUTPUT_BYTES = 256 * 1024 * 1024


class BackendError(Exception):
    """The backend refused or failed an operation (fail closed, no fallback)."""


class BackendUnavailableError(BackendError):
    """No usable backend runtime (missing daemon, unavailable resource)."""


class ProtocolError(BackendError):
    """A job message violated the structured protocol (size/depth/type/seq)."""


class CrossJobError(BackendError):
    """Output carried another job's identity or touched another job's state."""


def _check_limits(limits: Any) -> dict[str, int]:
    if type(limits) is not dict or set(limits) != {
            "wall_clock_ms", "max_output_bytes"}:
        raise ValueError(
            "limits must be exactly {wall_clock_ms, max_output_bytes}")
    wall = limits["wall_clock_ms"]
    out = limits["max_output_bytes"]
    if type(wall) is not int or not 1 <= wall <= _MAX_WALL_CLOCK_MS:
        raise ValueError("limits.wall_clock_ms out of range")
    if type(out) is not int or not 1 <= out <= _MAX_OUTPUT_BYTES:
        raise ValueError("limits.max_output_bytes out of range")
    return {"wall_clock_ms": wall, "max_output_bytes": out}


def _check_package(package: Any) -> dict[str, str]:
    if type(package) is not dict or set(package) != {"kind", "identity"}:
        raise ValueError("package must be exactly {kind, identity}")
    kind, identity = package["kind"], package["identity"]
    if type(kind) is not str or not kind or len(kind) > 64:
        raise ValueError("package.kind must be a short non-empty string")
    if type(identity) is not str or not identity or len(identity) > 256:
        raise ValueError("package.identity must be a non-empty string")
    return {"kind": kind, "identity": identity}


@dataclass(frozen=True)
class JobSpec:
    """Controller-assigned job identity, inputs, and budgets.

    ``job_id`` is assigned by the controller and is the only identity the
    backend trusts; job output claiming another id is rejected. ``package``
    is exactly ``{"kind", "identity"}`` where identity pins a prepared
    (validated, never executed) package. ``no_network`` defaults to True;
    backends MUST report their real network posture via capabilities().
    """

    job_id: str
    project_id: str
    package: dict = field(compare=False)
    adapter_ref: str = ""
    fixture: dict = field(default_factory=dict, compare=False)
    limits: dict = field(default_factory=dict, compare=False)
    no_network: bool = True

    def __post_init__(self) -> None:
        if (type(self.job_id) is not str
                or not _JOB_ID_PATTERN.match(self.job_id)):
            raise ValueError("invalid job_id")
        if (type(self.project_id) is not str
                or not _PROJECT_ID_PATTERN.match(self.project_id)):
            raise ValueError("invalid project_id")
        if (type(self.adapter_ref) is not str
                or not _ADAPTER_REF_PATTERN.match(self.adapter_ref)):
            raise ValueError("adapter_ref must look like 'module:Class'")
        if type(self.fixture) is not dict:
            raise ValueError("fixture must be an object")
        if type(self.no_network) is not bool:
            raise ValueError("no_network must be boolean")
        object.__setattr__(self, "package", _check_package(self.package))
        object.__setattr__(self, "limits", _check_limits(self.limits))
        object.__setattr__(self, "fixture", dict(self.fixture))


class ExecutionBackend:
    """Versioned isolated-execution interface (never import payload code)."""

    name: str = "base"
    version: str = BACKEND_VERSION

    def capabilities(self) -> dict:
        """Describe real posture (never claim unproven isolation).

        Returns exactly ``{"isolation": {"filesystem", "process",
        "network"}, "limits": [...], "qualification": {"status",
        "report_id"?}}``.
        """
        raise NotImplementedError

    def prepare(self, package: dict) -> dict:
        """Validate and pin a package; NEVER executes payload code.

        Returns ``{"identity", "provenance"}``. Raises BackendError on any
        validation failure.
        """
        raise NotImplementedError

    def start_job(self, spec: JobSpec) -> None:
        """Start a job from a controller-assigned spec (idempotent: no).

        The job_id comes from the spec; duplicate or unknown-package ids
        raise BackendError. Returns None.
        """
        raise NotImplementedError

    def send(self, job_id: str, command: dict) -> None:
        """Deliver one command (``{"type", "payload"}``) to a live job."""
        raise NotImplementedError

    def poll(self, job_id: str, timeout_ms: int = 0) -> list:
        """Return newly arrived validated events (possibly empty)."""
        raise NotImplementedError

    def status(self, job_id: str) -> dict:
        """Return ``{"state", ...}`` without blocking."""
        raise NotImplementedError

    def cancel(self, job_id: str, grace_ms: int = 2000) -> dict:
        """Terminate (grace then escalate) and return ``{state, confirmed}``."""
        raise NotImplementedError

    def close(self, job_id: str) -> dict:
        """Reap the job, remove its workspace, return ``{receipts}``."""
        raise NotImplementedError
