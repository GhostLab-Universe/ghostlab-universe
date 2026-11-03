"""Held-out evaluation harness (this side): generic runner plus result contract.

This package consumes a hidden scenario set it has never seen: the held-out
owner authors the scenario directory and manifest, and this side only defines
the shapes, the frozen-config handshake and the versioned result contract.
The only scenarios shipped here are the clearly-labeled SYNTHETIC self-test
fixture in ``selftest_fixture/``. Standard library only.
"""

from .heldout_runner import (HELDOUT_CONFIG_VERSION, HELDOUT_HANDSHAKE_VERSION,
                             HELDOUT_MANIFEST_VERSION, HELDOUT_NAMESPACE,
                             HELDOUT_SPEC_VERSION, SYNTHETIC_NAMESPACE,
                             VERDICT_PASS, VERDICT_UNKNOWN, VERDICT_VIOLATION,
                             FreezeMismatchError, FrozenConfig, HeldoutHandle,
                             HeldoutManifestError, HeldoutResultError,
                             HeldoutRunnerError, HeldoutSpecError,
                             NamespaceError, dump_result_json, freeze_config,
                             frozen_handshake, load_manifest, run_heldout,
                             run_selftest, to_legacy_result, validate_result,
                             verify_handshake)

__all__ = [
    "HELDOUT_CONFIG_VERSION", "HELDOUT_HANDSHAKE_VERSION",
    "HELDOUT_MANIFEST_VERSION", "HELDOUT_NAMESPACE", "HELDOUT_SPEC_VERSION",
    "SYNTHETIC_NAMESPACE", "VERDICT_PASS", "VERDICT_UNKNOWN",
    "VERDICT_VIOLATION", "FreezeMismatchError", "FrozenConfig",
    "HeldoutHandle", "HeldoutManifestError", "HeldoutResultError",
    "HeldoutRunnerError", "HeldoutSpecError", "NamespaceError",
    "dump_result_json", "freeze_config", "frozen_handshake", "load_manifest",
    "run_heldout", "run_selftest", "to_legacy_result", "validate_result",
    "verify_handshake",
]
