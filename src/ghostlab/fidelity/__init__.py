"""Simulation-fidelity records (Track E3).

Fidelity means (a) which faults the real service could exhibit and the
checked-in world models, and (b) whether the world's state transitions match
the real service's transitions for the declared tools.

``manifests`` carries assessed per-environment claims (synthetic versus
proxy-backed); ``diverge`` replays identical actions against two adapters to
check that claim behaviorally. Nothing here claims synthetic == production.
Standard library only; no model calls.
"""

from .diverge import compare_runs, fidelity_probe
from .manifests import (ASSESSED_MANIFESTS, BOOKING_FIDELITY_MANIFEST,
                        FIDELITY_MANIFEST_VERSION, PROXY_FIDELITY_TEMPLATE,
                        REFUND_FIDELITY_MANIFEST, FidelityStore,
                        canonical_digest, validate_fidelity_manifest,
                        validate_fidelity_manifest_bytes)

__all__ = [
    "ASSESSED_MANIFESTS",
    "BOOKING_FIDELITY_MANIFEST",
    "FIDELITY_MANIFEST_VERSION",
    "PROXY_FIDELITY_TEMPLATE",
    "REFUND_FIDELITY_MANIFEST",
    "FidelityStore",
    "canonical_digest",
    "compare_runs",
    "fidelity_probe",
    "validate_fidelity_manifest",
    "validate_fidelity_manifest_bytes",
]
