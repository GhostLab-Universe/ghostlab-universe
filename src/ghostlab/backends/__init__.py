"""Isolated execution backends (versioned interface + reference local).

The frozen surface lives in :mod:`interface` (errors, JobSpec, lifecycle,
ExecutionBackend) and :mod:`protocol` (strict JSON-line codec). :mod:`local`
is the unqualified OS-process reference used to validate protocol and
lifecycle handling only — never a sandbox. :mod:`intake` validates and
extracts archive packages without executing payload content.
"""

from .interface import (BACKEND_VERSION, STATES, TERMINAL_STATES,
                        BackendError, BackendUnavailableError, CrossJobError,
                        ExecutionBackend, JobSpec, ProtocolError)
from .local import LocalBackend

__all__ = [
    "BACKEND_VERSION", "STATES", "TERMINAL_STATES", "BackendError",
    "BackendUnavailableError", "CrossJobError", "ExecutionBackend",
    "JobSpec", "LocalBackend", "ProtocolError",
]
