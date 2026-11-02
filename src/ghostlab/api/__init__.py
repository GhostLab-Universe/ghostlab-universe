"""Live service package (Track E4).

The FastAPI service in :mod:`ghostlab.api.app` binds the guided UI's
``EffectiveConfig`` to real execution through Track C operations. This
package makes no model calls and holds no secrets.

There is deliberately **no environment-registration route**: installing
adapter code (``EnvRegistry.register``) resolves and fingerprints a
checked-in ``module:Class`` reference, which must stay an explicit local
CLI operator act (``ghostlab env-register``), never a remote API call that
could be driven at an operator's browser. The service only lists,
imports manifests for, discovers, qualifies and executes already
registered environments.
"""

from .app import create_app
from .guide import (DEFAULT_POLICY, GUIDE_POLICY_VERSION,
                    map_effective_to_requested, validate_effective_config)

__all__ = [
    "DEFAULT_POLICY",
    "GUIDE_POLICY_VERSION",
    "create_app",
    "map_effective_to_requested",
    "validate_effective_config",
]
