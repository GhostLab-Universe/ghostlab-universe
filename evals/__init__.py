"""M09 independent evaluations. Results come from stored artifacts, never model claims.

LangGraph pulls in LangSmith, so ``LANGSMITH_TRACING=true`` would post every run of this
suite while its report said no provider was contacted. Trace export is forced off here,
when the package loads and before anything can import LangGraph, whatever the environment
says, and the key is dropped from the process. ``ghostlab.api`` does the same on M07's
branch. ``evals.run`` repeats it in case the environment changed. This module imports
nothing else on purpose.
"""

import os
import sys

TRACING_VARIABLES = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING",
                     "LANGCHAIN_TRACING_V2")
TRACE_KEYS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")


def disable_trace_export() -> None:
    for name in TRACING_VARIABLES:
        os.environ[name] = "false"
    for name in TRACE_KEYS:
        os.environ.pop(name, None)
    utils = sys.modules.get("langsmith.utils")
    if utils is not None:  # it caches what it read from the environment
        utils.get_env_var.cache_clear()


disable_trace_export()
