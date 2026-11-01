"""M04 storage interfaces, version ``ghostlab-storage/1``.

Consumers in this wave: M03 reads memory through ``MemoryStore``; M06 persists
approval transitions through ``ApprovalStore``. Memory stays version 1 and gains
world records additively: ``put`` also takes ``WorldMemoryRecord``, ``query`` also
takes ``WorldMemoryQuery``, and non-refund rows come back as ``WorldMemoryRecord``
(a ``MemoryRecord`` subclass). Refund rows and callers are unchanged. Budget, artifact and checkpoint
interfaces follow in M04's implementation for M05.

Stores own transactions and isolation, never policy. They do not decide whether
an approval is valid or a memory record is trustworthy; they apply atomic
compare-and-set transitions and refuse cross-project access. Records are M00
contracts (``ghostlab-contracts/1``) and are returned re-validated, never built
with ``model_copy(update=...)``.
"""

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

from ghostlab.contracts import (ApprovalDecision, ApprovalRequest, MemoryQuery, MemoryRecord,
                                TrustedScope)
from ghostlab.contracts.memory import MemoryStatus

STORAGE_INTERFACE_VERSION = "ghostlab-storage/1"


class StorageError(Exception):
    """Base class for storage failures. Messages never include record content."""


class NotFound(StorageError):
    """Missing, or owned by another project (existence is not disclosed)."""


class ScopeViolation(StorageError):
    """A request named a project other than the caller's trusted scope."""


class Conflict(StorageError):
    """Duplicate identifier, or a compare-and-set precondition did not hold."""


class IntegrityError(StorageError):
    """Stored data failed validation or digest checks."""


@runtime_checkable
class MemoryStore(Protocol):
    def put(self, scope: TrustedScope, record: MemoryRecord) -> None:
        """Insert a record. ``record.project_id`` must equal ``scope.project_id``
        (else ScopeViolation); a reused ``record_id`` raises Conflict."""

    def get(self, scope: TrustedScope, record_id: str) -> MemoryRecord:
        """Return one record in the caller's project, else NotFound."""

    def query(self, scope: TrustedScope, query: MemoryQuery) -> Sequence[MemoryRecord]:
        """Selective retrieval within ``scope.project_id`` (else ScopeViolation).

        Returns records matching ``kinds``, ``statuses``, ``world_version`` (None =
        any) and ``campaign_id`` (None = any) that are effective at ``as_of``
        (effective_from <= as_of < effective_until) and not past
        ``retention.expires_at``. Ordered by observed_at descending, then
        record_id; at most ``limit`` records.
        """

    def transition(self, scope: TrustedScope, record_id: str, *,
                   expected_status: MemoryStatus, new_status: MemoryStatus) -> MemoryRecord:
        """Atomically move a record from ``expected_status`` to ``new_status``.

        Raises Conflict if the current status differs. The updated record is
        re-validated, so moving untrusted provenance to ``verified`` fails.
        """


@runtime_checkable
class ApprovalStore(Protocol):
    """Atomic approval persistence. M06 decides; this only records transitions.

    The project of an approval is ``request.scope.project_id``.
    """

    def create(self, request: ApprovalRequest) -> None:
        """Persist a ``pending`` request; a reused ``approval_id`` raises Conflict."""

    def get(self, project_id: str, approval_id: str) -> ApprovalRequest:
        """Current state, else NotFound (including other projects' approvals)."""

    def get_decision(self, project_id: str, approval_id: str) -> ApprovalDecision | None:
        """The recorded decision, if any."""

    def record_decision(self, project_id: str, decision: ApprovalDecision, *,
                        new_status: Literal["approved", "rejected"]) -> ApprovalRequest:
        """Atomically move ``pending`` -> ``new_status`` and store ``decision``.

        Raises Conflict unless the request is still pending and
        ``decision.action_digest`` equals the stored ``action_digest``.
        """

    def mark_expired(self, project_id: str, approval_id: str) -> ApprovalRequest:
        """Atomically move ``pending`` -> ``expired``; Conflict otherwise."""

    def consume(self, project_id: str, approval_id: str, action_digest: str) -> ApprovalRequest:
        """Single use: atomically move ``approved`` -> ``consumed``.

        Raises Conflict if not approved (including already consumed) or if
        ``action_digest`` differs. Exactly one concurrent caller can succeed.
        """


@runtime_checkable
class PendingApprovalStore(Protocol):
    """Optional bounded reviewer queue; does not expand the baseline ApprovalStore."""

    def list_pending(self, project_id: str, *, after: str = "", limit: int = 50) -> list[ApprovalRequest]:
        """Project-scoped pending approvals in id order after an exclusive cursor (1..100)."""
