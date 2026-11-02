"""FastAPI application factory (``ghostlab-api/1``).

``create_app()`` with no configuration serves the same contract and denies every
request: that is what generates the OpenAPI document. A configured app refuses a
hosted deployment or a non-loopback bind, because no production authentication exists.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ghostlab.security.redaction import RedactingFilter, Redactor
from ghostlab.storage.checkpoints import graph_checkpointer

from .errors import install
from .ext_plane import ExtensionPlane
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
            app.state.ext_plane = ExtensionPlane()
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
