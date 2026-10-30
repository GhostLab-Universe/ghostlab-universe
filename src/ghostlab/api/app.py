"""FastAPI application factory (``ghostlab-api/1``)."""

from fastapi import FastAPI

from .routes import router

API_VERSION = "1.0.0"
DESCRIPTION = ("Authenticated control plane for GhostLab Universe experiments. Worlds are "
               "synthetic. Project and principal come from the session, never a request. "
               "Events are application events, not model-token streaming.")


def create_app() -> FastAPI:
    app = FastAPI(title="GhostLab Control Plane", version=API_VERSION, description=DESCRIPTION,
                  docs_url=None, redoc_url=None, openapi_url=None,
                  separate_input_output_schemas=False)
    app.include_router(router)
    return app
