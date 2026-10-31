"""Nothing leaves this server: LangSmith trace export is off, whatever the environment says.

LangGraph pulls in LangSmith, and setting ``LANGSMITH_TRACING=true`` would make every graph
run post its state to LangSmith, with the key, the client's objective and the principal.
No destination or redaction has been approved for that, so ``ghostlab.api`` turns it off
when the package loads, before anything imports LangGraph, and again in ``create_app`` in
case the environment changed. This module imports nothing else on purpose.
"""

import os
import sys
from collections.abc import MutableMapping

TRACING_VARIABLES = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING",
                     "LANGCHAIN_TRACING_V2")
TRACE_KEYS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")
PROVIDER_KEYS = ("MODEL_API_KEY", "BASETEN_API_KEY")


def disable_trace_export(environ: MutableMapping[str, str] | None = None) -> None:
    environ = os.environ if environ is None else environ
    for name in TRACING_VARIABLES:
        environ[name] = "false"
    for name in TRACE_KEYS:
        environ.pop(name, None)
    utils = sys.modules.get("langsmith.utils")
    if utils is not None:  # it caches what it read from the environment
        utils.get_env_var.cache_clear()


def drop_provider_keys(environ: MutableMapping[str, str] | None = None) -> None:
    """Scripted mode needs no key, so the process holds none, even to redact."""
    environ = os.environ if environ is None else environ
    for name in PROVIDER_KEYS:
        environ.pop(name, None)
