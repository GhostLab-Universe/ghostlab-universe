"""M07 authenticated control plane (``ghostlab-api/1``). Requires the ``service`` extra."""

from .app import API_VERSION, create_app

__all__ = ["API_VERSION", "create_app"]
