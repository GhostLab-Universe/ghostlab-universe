"""Execution-boundary gate. Unknown code stays disabled until a backend qualifies.

M02 owns ``ExecutionBackend`` and its implementations; this module only decides
whether a given code origin may run on a given backend.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from .authorization import Decision, allow, deny

CodeOrigin = Literal["checked_in", "uploaded", "model_generated"]
BackendKind = Literal["trusted_local", "isolated"]

REQUIRED_ISOLATION_CHECKS: frozenset[str] = frozenset({
    "network_denied", "filesystem_confined", "resource_limits", "secrets_absent",
    "timeout_termination", "evidence_integrity",
})


@dataclass(frozen=True)
class BackendQualification:
    """Recorded isolation test evidence for one backend (check -> evidence reference)."""

    backend: str
    evidence: Mapping[str, str] = field(default_factory=dict)

    def missing(self) -> frozenset[str]:
        return frozenset(check for check in REQUIRED_ISOLATION_CHECKS
                         if not self.evidence.get(check))


def authorize_execution(origin: CodeOrigin, backend: BackendKind, *,
                        qualification: BackendQualification | None = None,
                        unknown_code_enabled: bool = False) -> Decision:
    if origin == "checked_in" and backend == "trusted_local":
        return allow("trusted checked-in adapter on the local backend")
    if backend == "trusted_local":
        return deny("capability_denied", "the trusted local backend is not an isolation boundary")
    if not unknown_code_enabled:
        return deny("capability_denied", "unknown-code execution is disabled")
    if qualification is None or qualification.missing():
        missing = sorted(REQUIRED_ISOLATION_CHECKS if qualification is None
                         else qualification.missing())
        return deny("capability_denied", f"backend lacks isolation evidence: {missing}")
    return allow(f"qualified isolated backend {qualification.backend!r}")
