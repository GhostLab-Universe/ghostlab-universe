"""The request boundary, ahead of routing: every path, including unknown ones.

Order: non-production boundary, session, body size. A request that fails any of them
never reaches a route, and no route has to remember to ask. Every protected route needs a session; only the exact login entry/callback/status paths are public,
though a ``local_dev`` one takes no credential. Starlette has no body limit of its own, so
this one reads at most ``limit`` bytes before anything parses them.
"""

import logging

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from ghostlab.capsule import MAX_CAPSULE_BYTES
from ghostlab.contracts import ErrorInfo
from ghostlab.security.redaction import Redactor

from .errors import ApiError, respond
from .models import ErrorResponse
from .sessions import SessionAdapter, request_problem
from .oauth import OAuthSessions

MAX_BODY_BYTES = 64 * 1024
MAX_IMPORT_BYTES = MAX_CAPSULE_BYTES + 4 * 1024  # a capsule plus its JSON framing
IMPORT_PATH = "/v1/capsules/import"
RESPONSE_HEADERS = ((b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"))

logger = logging.getLogger("ghostlab.api")


async def _read_body(scope, receive, limit: int) -> bytes | None:
    """The whole body, or ``None`` once it exceeds ``limit``."""
    declared = Headers(scope=scope).get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > limit:
        return None
    body, more = b"", True
    while more:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        body += message.get("body", b"")
        more = message.get("more_body", False)
        if len(body) > limit:
            return None
    return body


class RequestGuard:
    def __init__(self, app, sessions: SessionAdapter, redactor: Redactor):
        self.app, self.sessions, self.redactor = app, sessions, redactor

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return  # no websockets: an unaccepted connection is closed by the server

        started = False

        async def guarded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = {**message, "headers": [*message.get("headers", ()),
                                                  *RESPONSE_HEADERS]}
            await send(message)

        async def refuse(error: ApiError):
            await respond(error, self.redactor)(scope, receive, guarded_send)

        headers = Headers(scope=scope)
        if (problem := request_problem(scope.get("client"), headers)) is not None:
            await refuse(ApiError(403, "forbidden", problem, reason="not_loopback"))
            return
        public_auth = scope["method"] == "GET" and scope["path"] in {
            "/v1/auth/status", "/v1/auth/github/start", "/v1/auth/google/start",
            "/v1/auth/github/callback", "/v1/auth/google/callback"}
        if public_auth:
            await self.app(scope, receive, guarded_send)
            return
        if isinstance(self.sessions, OAuthSessions) and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            if headers.get("origin") != self.sessions.origin:
                await refuse(ApiError(403, "forbidden", "Request origin does not match this workspace", reason="invalid_origin"))
                return
        session = self.sessions.authenticate(headers)
        if session is None:
            await refuse(ApiError(401, "unauthenticated", "A valid session is required",
                                  headers={"WWW-Authenticate": "Bearer"}))
            return
        scope.setdefault("state", {})["session"] = session

        downstream = receive
        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            limit = MAX_IMPORT_BYTES if scope["path"] == IMPORT_PATH else MAX_BODY_BYTES
            body = await _read_body(scope, receive, limit)
            if body is None:
                await refuse(ApiError(413, "validation_failed", "Request body too large",
                                      reason="too_large"))
                return
            delivered = False

            async def downstream():
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}

        try:
            await self.app(scope, downstream, guarded_send)
        except Exception as error:  # not re-raised: the server would log the raw traceback
            logger.error("Unhandled API failure", exc_info=error)
            if started:
                raise
            info = ErrorInfo(code="internal", message="Internal error", retryable=False,
                             side_effect="unknown", ref=None)
            body = ErrorResponse(error=info, reason=None).model_dump(mode="json")
            await JSONResponse(body, status_code=500)(scope, receive, guarded_send)
