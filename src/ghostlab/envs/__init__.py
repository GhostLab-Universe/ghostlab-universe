"""Trusted local environment integration (Track A).

Explicit registration of reviewed, checked-in local adapter code; bounded JSON
manifest import; capability discovery; an explicit qualification runner; and
version-bound reports. Standard library only, like ``ghostlab.core``.

Nothing here executes uploaded code, installs packages, or fetches URLs.
Manifest validation never imports or runs an environment; only the explicit
capability-discovery and qualification operations instantiate adapters, and
only through allowlisted ``ghostlab.*`` references.
"""

ENVS_SCHEMA_VERSION = 1
QUALIFICATION_RUNNER_VERSION = "env-qualification/1"

STATUS_NOT_QUALIFIED = "not-qualified"
STATUS_QUALIFIED = "qualified-for-declared"
STATUS_FAILED = "failed"

STATUSES = (STATUS_NOT_QUALIFIED, STATUS_QUALIFIED, STATUS_FAILED)

__all__ = [
    "ENVS_SCHEMA_VERSION",
    "QUALIFICATION_RUNNER_VERSION",
    "STATUS_FAILED",
    "STATUS_NOT_QUALIFIED",
    "STATUS_QUALIFIED",
    "STATUSES",
]
