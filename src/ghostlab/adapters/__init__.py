"""Trusted checked-in local adapter packages and the operator registry.

Only reviewed code in this package registers here, by calling
:func:`register_refund_adapters` with an explicit registry. Factories are
plain local references: nothing is imported by name from a proposal, capsule
or request, no path is ever loaded or executed, and the browser never runs
uploaded Python. Standard library only.
"""

from functools import partial

from ghostlab.core import ADAPTER_VERSION
from ghostlab.extensions.registry import AdapterManifest, AdapterRegistry

from .refund_env import (ENVIRONMENT_ID, ENVIRONMENT_VERSION, REFUND_WORLD_VERSION,
                         SOURCE, RefundEnvironment, ledger_change_events)
from .refund_target import ADAPTER_IDS, REPAIRED, VULNERABLE, RefundTarget

ENVIRONMENT_MANIFEST = AdapterManifest(
    package="ghostlab.adapters.refund_env", version=ENVIRONMENT_VERSION,
    origin="checked_in", description="Synthetic INR 2,500 refund world")
TARGET_MANIFEST = AdapterManifest(
    package="ghostlab.adapters.refund_target", version=ADAPTER_VERSION,
    origin="checked_in", description="Deterministic refund targets (vulnerable/repaired)")


def register_refund_adapters(registry: AdapterRegistry | None = None) -> AdapterRegistry:
    """Register the checked-in refund adapters on a registry (a fresh one by default)."""
    registry = registry if registry is not None else AdapterRegistry()
    registry.register_environment(REFUND_WORLD_VERSION, RefundEnvironment,
                                  ENVIRONMENT_MANIFEST)
    for adapter_id in ADAPTER_IDS:
        registry.register_target(REFUND_WORLD_VERSION, adapter_id,
                                 partial(RefundTarget, adapter_id), TARGET_MANIFEST)
    return registry


_DEFAULT: AdapterRegistry | None = None


def default_registry() -> AdapterRegistry:
    """The process operator registry, with the checked-in adapters registered."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = register_refund_adapters()
    return _DEFAULT


__all__ = [
    "ADAPTER_IDS", "ENVIRONMENT_ID", "ENVIRONMENT_MANIFEST", "ENVIRONMENT_VERSION",
    "REPAIRED", "REFUND_WORLD_VERSION", "SOURCE", "TARGET_MANIFEST", "VULNERABLE",
    "RefundEnvironment", "RefundTarget", "default_registry", "ledger_change_events",
    "register_refund_adapters",
]