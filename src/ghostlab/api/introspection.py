"""RFC 7662 authentication; deployment remains loopback until separately qualified.

Tokens are opaque. Only the configured HTTPS authorization server interprets them.
Project membership comes from operator configuration, never token/body project claims.
No token cache: issuer revocation takes effect on the next request.
"""

import json
import dataclasses
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx
from starlette.datastructures import Headers

from ghostlab.contracts import Principal
from ghostlab.policy import root_scope

from .sessions import ApiSession

MAX_RESPONSE_BYTES = 65_536
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*", re.ASCII)


def _https(value: str) -> None:
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and url.hostname and not url.username
                 and not url.password and not url.query and not url.fragment and url.port != 0)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("identity provider URLs must be explicit HTTPS URLs")


@dataclass(frozen=True)
class IntrospectionConfig:
    issuer: str
    endpoint: str
    audience: str
    client_id: str
    client_secret: str = field(repr=False)
    required_scope: str = "ghostlab"

    def __post_init__(self):
        _https(self.issuer)
        _https(self.endpoint)
        if any(type(v) is not str or not v or len(v) > 4096 for v in
               (self.audience, self.client_id, self.client_secret, self.required_scope)):
            raise ValueError("identity provider configuration is incomplete")


@dataclass(frozen=True)
class ProjectMember:
    principal: Principal
    project_id: str

    def __post_init__(self):
        root_scope(self.project_id, self.principal)  # validate trusted identifiers
        if self.principal.kind != "human":
            raise ValueError("interactive authentication requires a human member")


class IntrospectionSessions:
    """SessionAdapter for a configured IdP, not an account-registration/login server.

    ``members`` is issuer subject -> one server-assigned principal/project. Multiple
    projects require distinct sessions; no request header selects a project. The
    existing request middleware still enforces loopback, including Origin and Host.
    ``transport`` is for isolated tests. Real clients validate TLS, ignore environment
    proxies, never follow redirects, and never retry authentication requests.
    """

    def __init__(self, config: IntrospectionConfig, members: Mapping[str, ProjectMember], *,
                 transport=None, clock=time.time, monotonic=time.monotonic):
        if not all(type(k) is str and 0 < len(k) <= 256 and isinstance(v, ProjectMember)
                   for k, v in members.items()):
            raise ValueError("invalid identity-provider membership configuration")
        self.config = config
        self._members = MappingProxyType(dict(members))
        self._clock, self._monotonic = clock, monotonic
        self._slots = threading.BoundedSemaphore(8)
        self._client = httpx.Client(transport=transport, timeout=5.0, trust_env=False,
                                    follow_redirects=False)

    def close(self):
        self._client.close()

    @classmethod
    def from_environment(cls, members, *, environ=None, **options):
        env = os.environ if environ is None else environ
        config = IntrospectionConfig(
            issuer=env.get("GHOSTLAB_OIDC_ISSUER", ""),
            endpoint=env.get("GHOSTLAB_OIDC_INTROSPECTION_URL", ""),
            audience=env.get("GHOSTLAB_OIDC_AUDIENCE", ""),
            client_id=env.get("GHOSTLAB_OIDC_CLIENT_ID", ""),
            client_secret=env.get("GHOSTLAB_OIDC_CLIENT_SECRET", ""))
        return cls(config, members, **options)

    def authenticate(self, headers: Headers) -> ApiSession | None:
        auth = headers.getlist("authorization")
        if len(auth) != 1:
            return None
        scheme, _, token = auth[0].partition(" ")
        if (scheme.lower() != "bearer" or not 1 <= len(token) <= 8192
                or _TOKEN.fullmatch(token) is None or not self._slots.acquire(blocking=False)):
            return None
        try:
            started = self._monotonic()
            with self._client.stream("POST", self.config.endpoint,
                                     auth=(self.config.client_id, self.config.client_secret),
                                     data={"token": token, "token_type_hint": "access_token"},
                                     headers={"Accept": "application/json"}) as response:
                if response.status_code != 200:
                    return None
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES or self._monotonic() - started > 5:
                        return None
            claims = json.loads(content)
            return self._session(claims)
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError):
            return None  # fail closed; do not log a response that may echo the token
        finally:
            self._slots.release()

    def _session(self, claims) -> ApiSession | None:
        if type(claims) is not dict or claims.get("active") is not True:
            return None
        now = self._clock()
        if (claims.get("iss") != self.config.issuer or type(claims.get("exp")) is not int
                or claims["exp"] <= now):
            return None
        for name in ("nbf", "iat"):
            if name in claims and (type(claims[name]) is not int or claims[name] > now):
                return None
        aud = claims.get("aud")
        if not ((type(aud) is str and aud == self.config.audience)
                or (type(aud) is list and all(type(x) is str for x in aud)
                    and self.config.audience in aud)):
            return None
        scope = claims.get("scope")
        if type(scope) is not str or self.config.required_scope not in scope.split():
            return None
        token_type = claims.get("token_type", "Bearer")
        if type(token_type) is not str or token_type.lower() != "bearer":
            return None
        subject = claims.get("sub")
        if type(subject) is not str or subject not in self._members:
            return None
        member = self._members[subject]
        return ApiSession(member.principal, member.project_id, "oidc_introspection")


def configure_introspection(config, sessions: IntrospectionSessions, *, approvers,
                            retention=None, data_classification="unclassified"):
    """Install IdP sessions and independent review without weakening local isolation.

    This is host configuration, not a public endpoint. Human memberships and world
    bindings must be configured by the operator. Live inference additionally needs
    the existing M01/M05 authorization and a matching retention decision.
    """
    from ghostlab.policy.separation import SeparatedApprovalService
    from ghostlab.security.retention import RetentionCheckedFactory
    services = config.services
    factory = services.adapter_factory
    if services.live is not None:
        if retention is None:
            raise ValueError("live inference needs a retention decision")

        factory = RetentionCheckedFactory(factory, retention, data_classification, services.clock)

    services = dataclasses.replace(
        services, adapter_factory=factory,
        approvals=SeparatedApprovalService(services.approval_store, approvers=approvers))
    return dataclasses.replace(config, services=services, sessions=sessions, approvers=approvers)
