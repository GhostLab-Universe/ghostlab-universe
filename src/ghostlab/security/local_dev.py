"""Trusted-local development boundary. This is not production authentication.

Local-dev mode is an explicit opt-in (``GHOSTLAB_AUTH_MODE=local_dev``), binds
only to loopback, is refused outright in a hosted deployment, and yields a
principal of kind ``local_dev`` that production paths can recognise and reject.
"""

import ipaddress
import os
from collections.abc import Mapping

from ghostlab.contracts import Principal

LOCAL_DEV_PRINCIPAL_ID = "local-dev-user"
_LOOPBACK_NAMES = {"localhost"}


class LocalDevRefused(Exception):
    pass


def is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def local_dev_enabled(environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get("GHOSTLAB_AUTH_MODE") == "local_dev"


def require_local_dev(bind_host: str, environ: Mapping[str, str] | None = None) -> Principal:
    """Return the local-dev principal, or refuse. M07 calls this at startup."""
    environ = os.environ if environ is None else environ
    if environ.get("GHOSTLAB_DEPLOYMENT", "local") != "local":
        raise LocalDevRefused("local_dev mode is never allowed in a hosted deployment")
    if not local_dev_enabled(environ):
        raise LocalDevRefused("set GHOSTLAB_AUTH_MODE=local_dev to opt in explicitly")
    if not is_loopback(bind_host):
        raise LocalDevRefused(f"local_dev mode binds to loopback only, not {bind_host!r}")
    return Principal.model_validate({"principal_id": LOCAL_DEV_PRINCIPAL_ID, "kind": "local_dev"})


def is_production_authenticated(principal: Principal) -> bool:
    """No production authentication exists yet, so nothing qualifies; local_dev never will."""
    del principal
    return False
