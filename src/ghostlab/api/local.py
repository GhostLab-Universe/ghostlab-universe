"""Local-development wiring: real stores, the local-dev session, no model.

This is not a production configuration. No provider credential is read and no model is
reachable. By default creating a campaign is refused as ``inference_not_authorized``, and
reads, approvals over existing records, capsule import and replay all work. The explicit
``scripted`` opt-in serves checked-in proposals in process (``ghostlab.api.scripted``).
"""

from datetime import datetime, timezone
from pathlib import Path

from ghostlab.core import Fixture
from ghostlab.orchestration import Services
from ghostlab.orchestration.inference import LiveInferenceRefused
from ghostlab.policy import (ApprovalService, ProjectApproverAllowlist, StaticPrincipalBinding,
                             StaticWorldBinding)
from ghostlab.providers import Runtime
from ghostlab.skills import SkillRegistry
from ghostlab.storage.approval_store import SqliteApprovalStore
from ghostlab.storage.artifacts import FileArtifactStore
from ghostlab.storage.budgets import SqliteBudgetLedger
from ghostlab.storage.database import StoragePaths, open_database
from ghostlab.storage.memory_store import SqliteMemoryStore
from ghostlab.worlds.journey import WORLD_VERSION as JOURNEY_WORLD
from ghostlab.worlds.journey import JourneyFixture

from .plane import ApiConfig
from .scripted import scripted_adapter, scripted_runtime
from .sessions import ApiSession, LocalDevSessions
from .oauth import OAuthSessions


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _inference_disabled(runtime):
    raise LiveInferenceRefused("this server has no provider configured")


def local_config(root: Path, sessions: LocalDevSessions | OAuthSessions, *, bind_host: str,
                 scripted: bool = False, environ: dict | None = None) -> ApiConfig:
    """The trusted host's wiring: the local principal, bound to the synthetic actors."""
    paths = StoragePaths(root)
    app_db = open_database(paths.app_db)
    assignments = (list(sessions.members.values()) if isinstance(sessions, OAuthSessions)
                   else [ApiSession(sessions.principal, sessions.project_id, "local_dev")])
    allowed = {}
    for assignment in assignments:
        allowed.setdefault(assignment.project_id, set()).add(assignment.principal.principal_id)
    approval_store = SqliteApprovalStore(app_db)
    approvers = ProjectApproverAllowlist(allowed)
    services = Services(
        memory_store=SqliteMemoryStore(app_db), approval_store=approval_store,
        approvals=ApprovalService(approval_store, approvers=approvers, local_dev_mode=True),
        ledger=SqliteBudgetLedger(app_db), artifacts=FileArtifactStore(app_db, paths.artifacts),
        skills=SkillRegistry.load(),
        binding=StaticPrincipalBinding({(s.project_id, s.principal.principal_id):
                                        Fixture().customer_id for s in assignments}),
        world_binding=StaticWorldBinding({(s.project_id, s.principal.principal_id, JOURNEY_WORLD):
                                          JourneyFixture().organizer for s in assignments}),
        adapter_factory=scripted_adapter if scripted else _inference_disabled,
        runtime=scripted_runtime() if scripted else Runtime(), clock=_utc_now)
    return ApiConfig(services=services, sessions=sessions, approvers=approvers,
                     graph_db=paths.graph_db, app_db=paths.app_db, bind_host=bind_host,
                     environ=environ, scripted=scripted)
