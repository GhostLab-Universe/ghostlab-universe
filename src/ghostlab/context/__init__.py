"""M03 role-scoped context compilation (requires the `contracts` extra)."""

from .compiler import (CompiledContext, ContextCompiler, ContextError, ContextRequest,
                       MemorySelection, Observation, Omission, compile_context)
from .summary import SummaryResult, summarize_findings
from .tokens import estimate_tokens

__all__ = ["CompiledContext", "ContextCompiler", "ContextError", "ContextRequest",
           "MemorySelection", "Observation", "Omission", "SummaryResult", "compile_context",
           "estimate_tokens", "summarize_findings"]
