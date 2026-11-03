"""FastAPI application factory (``ghostlab-api/1``).

``create_app()`` with no configuration serves the same contract and denies every
request: that is what generates the OpenAPI document. A configured app refuses a
hosted deployment or a non-loopback bind, because no production authentication exists.
"""

import logging
import sqlite3
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ghostlab.security.redaction import RedactingFilter, Redactor
from ghostlab.storage.checkpoints import graph_checkpointer

from .errors import install
from .exp_plane import ExperimentPlane, default_experiment_policy
from .ext_plane import ExtensionPlane, default_registry
from ghostlab.adapters.refund_env import RefundEnvironment
from ghostlab.adapters.reservation_env import ReservationEnvironment
from ghostlab.experiments.store import ExperimentStore
from ghostlab.extensions.operator_registry import OperatorRegistry
from .ext_routes import router as ext_router
from .middleware import RequestGuard
from .plane import ApiConfig, ControlPlane
from .rootlock import RootLock, require_one_root
from .routes import router
from .sessions import NoSessions, require_local_deployment
from .tracing import disable_trace_export

API_VERSION = "1.0.0"
DESCRIPTION = ("Local control plane for GhostLab Universe experiments. No production "
               "authentication exists: a local_dev session takes no credential. Worlds are "
               "synthetic. Project and principal come from the session, never a request. "
               "Events are application events, not model-token streaming.")


def _attach_log_redaction(redactor: Redactor) -> None:
    logger = logging.getLogger("ghostlab.api")
    if not any(isinstance(item, RedactingFilter) for item in logger.filters):
        logger.addFilter(RedactingFilter(redactor))


_BUILTIN_ADAPTERS = (
    ("ghostlab.adapters.refund_env:RefundEnvironment", RefundEnvironment),
    ("ghostlab.adapters.reservation_env:ReservationEnvironment", ReservationEnvironment),
)


def seed_builtin_registrations(operators: OperatorRegistry) -> None:
    """Seed built-in registrations on a fresh registry. Idempotent."""
    for adapter_ref, factory in _BUILTIN_ADAPTERS:
        adapter = factory()
        try:
            description = adapter.describe()
        finally:
            adapter.close()
        try:
            operators.get(description.world_version)
            continue
        except LookupError:
            pass
        declared_tools = []
        for tool in description.tools:
            parameters = tool.parameters
            if isinstance(parameters, Mapping):
                properties = parameters.get("properties")
                if isinstance(properties, Mapping):
                    names = sorted(properties)
                else:
                    required = parameters.get("required")
                    names = sorted(required) if isinstance(required, (list, tuple)) else []
            else:
                names = []
            declared_tools.append({"name": tool.name, "parameters": names})
        try:
            operators.register(
                world_version=description.world_version,
                environment_id=description.environment_id,
                environment_version=description.environment_version,
                adapter_ref=adapter_ref,
                display_name=description.environment_id,
                declared_tools=declared_tools,
                declared_modes=["recorded-actions"],
                registered_by="builtin")
        except ValueError:
            try:
                operators.get(description.world_version)
            except LookupError:
                raise
        except sqlite3.OperationalError as error:
            raise RuntimeError(
                "operator registry is not writable; fix the storage root "
                "permissions or point the server at a writable root") from error


def create_app(config: ApiConfig | None = None) -> FastAPI:
    if config is not None:
        require_local_deployment(config.bind_host, config.environ)
        services = config.services
        require_one_root(config.graph_db, config.app_db, services.approval_store.path,
                         services.ledger.path, services.memory_store.path,
                         services.artifacts.db_path, services.artifacts.root)
        disable_trace_export()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if config is None:
            yield
            return
        # The only way to a ControlPlane, so every entry point holds the root's lock.
        with RootLock(config.graph_db.parent), graph_checkpointer(
                config.graph_db, app_db=config.app_db) as saver:
            plane = app.state.plane = ControlPlane(config, saver)
            shared = default_registry()
            app.state.ext_plane = ExtensionPlane(shared)
            root = config.graph_db.parent
            operators = OperatorRegistry(root / "operator_registry.sqlite")
            seed_builtin_registrations(operators)
            app.state.exp_plane = ExperimentPlane(
                store=ExperimentStore(root / "experiments.sqlite"),
                operators=operators,
                targets=shared,
                policy=config.exp_policy or default_experiment_policy(shared))
            try:
                yield
            finally:
                plane.jobs.shutdown()

    app = FastAPI(title="GhostLab Control Plane", version=API_VERSION, description=DESCRIPTION,
                  docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
                  telemetry={"auto_configure": False},
                  separate_input_output_schemas=False)
    redactor = config.redactor if config else Redactor()
    _attach_log_redaction(redactor)
    install(app, redactor)
    app.add_middleware(RequestGuard, sessions=config.sessions if config else NoSessions(),
                       redactor=redactor)
    app.include_router(router)
    app.include_router(ext_router)
    return app
