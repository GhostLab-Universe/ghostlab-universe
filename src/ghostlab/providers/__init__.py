"""M01 provider adapters (interface ``ghostlab-providers/1``; requires the `service` extra).

No module here dispatches tools, reads keys outside ``Credential.from_env`` or
makes a network call unless an adapter's ``complete`` is invoked.
"""

from .base import (PROVIDER_INTERFACE_VERSION, CancelHook, Credential, ProviderAdapter,
                   ProviderConfig, ProviderConfigError, ProviderRequestError, RetryPolicy,
                   redact)
from .baseten_chat import BasetenChatAdapter
from .http import Attempt, Runtime
from .meta_responses import MetaResponsesAdapter

ADAPTERS = {"meta_responses": MetaResponsesAdapter,
            "baseten_chat_completions": BasetenChatAdapter}


def adapter_for(config: ProviderConfig, runtime: Runtime | None = None) -> ProviderAdapter:
    """The adapter for exactly the configured provider; there is no fallback."""
    return ADAPTERS[config.provider](config, runtime)


__all__ = ["ADAPTERS", "PROVIDER_INTERFACE_VERSION", "Attempt", "BasetenChatAdapter",
           "CancelHook", "Credential", "MetaResponsesAdapter", "ProviderAdapter",
           "ProviderConfig", "ProviderConfigError", "ProviderRequestError", "RetryPolicy",
           "Runtime", "adapter_for", "redact"]
