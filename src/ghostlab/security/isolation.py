"""Data-isolation policy: every read or write is confined to the scope's project."""

from ghostlab.contracts import TrustedScope


class IsolationViolation(Exception):
    pass


def require_same_project(scope: TrustedScope, project_id: str) -> None:
    if scope.project_id != project_id:
        raise IsolationViolation("resource belongs to a different project")


def visible(scope: TrustedScope, records, project_of) -> list:
    """Filter records to the scope's project; callers must still query scoped stores."""
    return [record for record in records if project_of(record) == scope.project_id]
