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
from .middleware import RequestGuard
from .plane import ApiConfig, ControlPlane
from .routes import router
from .sessions import NoSessions, require_local_deployment

API_VERSION = "1.0.0"
DESCRIPTION = ("Authenticated control plane for GhostLab Universe experiments. Worlds are "
               "synthetic. Project and principal come from the session, never a request. "
               "Events are application events, not model-token streaming.")


def _attach_log_redaction(redactor: Redactor) -> None:
    logger = logging.getLogger("ghostlab.api")
    if not any(isinstance(item, RedactingFilter) for item in logger.filters):
        logger.addFilter(RedactingFilter(redactor))


def create_app(config: ApiConfig | None = None) -> FastAPI:
    if config is not None:
        require_local_deployment(config.bind_host, config.environ)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if config is None:
            yield
            return
        with graph_checkpointer(config.graph_db, app_db=config.app_db) as saver:
            plane = app.state.plane = ControlPlane(config, saver)
            try:
                yield
            finally:
                plane.jobs.shutdown()

    app = FastAPI(title="GhostLab Control Plane", version=API_VERSION, description=DESCRIPTION,
                  docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
                  separate_input_output_schemas=False)
    redactor = config.redactor if config else Redactor()
    _attach_log_redaction(redactor)
    install(app, redactor)
    app.add_middleware(RequestGuard, sessions=config.sessions if config else NoSessions(),
                       redactor=redactor)
    app.include_router(router)
    return app
