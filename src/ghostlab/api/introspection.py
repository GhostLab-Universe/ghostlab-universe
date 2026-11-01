"""RFC 7662 authentication; deployment remains loopback until separately qualified.

Tokens are opaque. Only the configured HTTPS authorization server interprets them.
Project membership comes from operator configuration, never token/body project claims.
No token cache: issuer revocation takes effect on the next request.
"""

import json
import asyncio
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
from .errors import ApiError

MAX_RESPONSE_BYTES = 65_536
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*", re.ASCII)


def _https(value: str) -> None:
    try:
        if type(value) is not str or any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError("invalid URL characters")
        url = urlsplit(value)
        httpx.URL(value)  # HTTPX must accept the same configured URL before any request.
        valid = (url.scheme == "https" and url.hostname and not url.username
                 and not url.password and not url.query and not url.fragment and url.port != 0)
    except (TypeError, ValueError, httpx.InvalidURL):
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
        self._transport = transport
        self._closed = False
        self._deadline_seconds = 5.0

    def close(self):
        # Clients are request-scoped and closed even on cancellation.
        self._closed = True

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
        """Synchronous protocol compatibility; ASGI uses authenticate_async directly."""
        return asyncio.run(self.authenticate_async(headers))

    async def authenticate_async(self, headers: Headers) -> ApiSession | None:
        auth = headers.getlist("authorization")
        if len(auth) != 1:
            return None
        scheme, _, token = auth[0].partition(" ")
        if (scheme.lower() != "bearer" or not 1 <= len(token) <= 8192
                or _TOKEN.fullmatch(token) is None):
            return None
        if self._closed or not self._slots.acquire(blocking=False):
            raise self._unavailable("authentication_capacity")
        try:
            # The outer cancellation covers DNS, connect, headers and all body reads.
            # No blocking IdP operation is placed on the thread pool.
            async with asyncio.timeout(self._deadline_seconds):
                async with httpx.AsyncClient(
                        transport=self._transport, timeout=self._deadline_seconds,
                        trust_env=False, follow_redirects=False) as client:
                    async with client.stream("POST", self.config.endpoint,
                            auth=(self.config.client_id, self.config.client_secret),
                            data={"token": token, "token_type_hint": "access_token"},
                            headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
                        if response.status_code != 200:
                            raise self._unavailable("authentication_provider")
                        # Reject compression before HTTPX can invoke a content decoder.
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise self._unavailable("authentication_response")
                        declared = response.headers.get("content-length")
                        if declared and (not declared.isascii() or not declared.isdigit()
                                         or len(declared) > 10 or int(declared) > MAX_RESPONSE_BYTES):
                            raise self._unavailable("authentication_response")
                        content = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=4096):
                            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise self._unavailable("authentication_response")
                            content.extend(chunk)
                        # Bound JSON nesting independently of Python's recursion limit.
                        depth, quoted, escaped = 0, False, False
                        for byte in content:
                            if quoted:
                                if escaped:
                                    escaped = False
                                elif byte == 92:
                                    escaped = True
                                elif byte == 34:
                                    quoted = False
                            elif byte == 34:
                                quoted = True
                            elif byte in (91, 123):
                                depth += 1
                                if depth > 64:
                                    raise self._unavailable("authentication_response")
                            elif byte in (93, 125):
                                depth -= 1
                        claims = json.loads(content)
                        return self._session(claims)
        except (TimeoutError, httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError, UnicodeError,
                RecursionError) as error:
            raise self._unavailable("authentication_provider") from error
        finally:
            self._slots.release()

    @staticmethod
    def _unavailable(reason):
        return ApiError(503, "internal", "Authentication service unavailable; retry later",
                        reason=reason, retryable=True, headers={"Retry-After": "1"})

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
        token_type = claims.get("token_type")
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
    bindings must be configured by the operator. Live inference through this draft
    API remains disabled; the optional retention record validates metadata only.
    """
    from ghostlab.policy.separation import SeparatedApprovalService
    from ghostlab.security.retention import RetentionCheckedFactory, RetentionRefused
    services = config.services
    factory = services.adapter_factory
    if services.live is not None:
        if retention is None:
            raise RetentionRefused("live inference needs a retention decision")

        factory = RetentionCheckedFactory(factory, retention, data_classification, services.clock)

    services = dataclasses.replace(
        services, adapter_factory=factory,
        approvals=SeparatedApprovalService(services.approval_store, approvers=approvers))
    return dataclasses.replace(config, services=services, sessions=sessions, approvers=approvers)
