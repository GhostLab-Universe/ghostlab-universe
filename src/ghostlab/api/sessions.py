"""Session adapters (M07 owns these; M06 owns what a principal may do).

A session resolves to one principal in one project, on the server, from the
credential alone. Nothing in a request can choose a project, principal, role or scope.

**Neither adapter here is production authentication.** ``LocalDevSessions`` is
M06's explicit local-development mode. ``TokenSessions`` is a set of operator-configured
bearer tokens, for exercising more than one identity on one machine. Both are refused
in a hosted deployment and served to loopback clients only (``request_problem``), and
``/v1/session`` reports ``production_authenticated: false``.
"""

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from starlette.datastructures import Headers

from ghostlab.contracts import Principal
from ghostlab.orchestration import TrustedSession
from ghostlab.security.local_dev import LocalDevRefused, is_loopback, require_local_dev

from .models import AuthMode

DEFAULT_PROJECT = "project-local"


@dataclass(frozen=True)
class ApiSession:
    principal: Principal
    project_id: str
    auth_mode: AuthMode

    @property
    def trusted(self) -> TrustedSession:
        return TrustedSession(self.principal, self.project_id)


@runtime_checkable
class SessionAdapter(Protocol):
    def authenticate(self, headers: Headers) -> ApiSession | None: ...


class NoSessions:
    """Deny everything: an app built without configuration serves nobody."""

    def authenticate(self, headers: Headers) -> ApiSession | None:
        return None


class LocalDevSessions:
    """The single local-development principal. Not production authentication."""

    def __init__(self, principal: Principal, project_id: str):
        self._session = ApiSession(principal, project_id, "local_dev")

    @classmethod
    def from_environment(cls, bind_host: str, *, project_id: str = DEFAULT_PROJECT,
                         environ: Mapping[str, str] | None = None) -> "LocalDevSessions":
        """Refuses unless M06's opt-in, deployment and loopback conditions all hold."""
        return cls(require_local_dev(bind_host, environ), project_id)

    @property
    def principal(self) -> Principal:
        return self._session.principal

    @property
    def project_id(self) -> str:
        return self._session.project_id

    def authenticate(self, headers: Headers) -> ApiSession | None:
        return self._session


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenSessions:
    """Operator-configured bearer tokens. Not production authentication.

    Only digests are kept, and a lookup compares digests, not the token.
    """

    def __init__(self, tokens: Mapping[str, tuple[Principal, str]]):
        self._sessions = {_digest(token): ApiSession(principal, project, "configured_tokens")
                          for token, (principal, project) in tokens.items()}

    def authenticate(self, headers: Headers) -> ApiSession | None:
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return self._sessions.get(_digest(token.strip()))


def _hostname(value: str) -> str | None:
    try:
        return urlsplit(value if "//" in value else f"//{value}").hostname
    except ValueError:
        return None


def request_problem(client: tuple[str, int] | None, headers: Headers) -> str | None:
    """Why a non-production session must not serve this request, if it must not.

    The peer must be loopback. The Host and any Origin must be too: otherwise a page on
    another site, or a rebound DNS name, could drive a server listening on 127.0.0.1.
    """
    if client is None or not is_loopback(client[0]):
        return "this server accepts loopback clients only"
    if not is_loopback(_hostname(headers.get("host", "")) or ""):
        return "this server accepts loopback Host headers only"
    origin = headers.get("origin")
    if origin is not None and not is_loopback(_hostname(origin) or ""):
        return "this server accepts loopback origins only"
    return None


def require_local_deployment(bind_host: str, environ: Mapping[str, str] | None = None) -> None:
    """No adapter here is production authentication, so none may be hosted or exposed."""
    environ = os.environ if environ is None else environ
    if environ.get("GHOSTLAB_DEPLOYMENT", "local") != "local":
        raise LocalDevRefused("no production authentication exists; refusing a hosted deployment")
    if not is_loopback(bind_host):
        raise LocalDevRefused(f"this server binds to loopback only, not {bind_host!r}")
