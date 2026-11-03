"""M07 control plane (``ghostlab-api/1``). Requires the ``service`` extra."""

from .tracing import disable_trace_export

disable_trace_export()  # before the next import brings in LangGraph and LangSmith

from .app import API_VERSION, create_app  # noqa: E402

__all__ = ["API_VERSION", "create_app"]
