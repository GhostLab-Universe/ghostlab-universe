"""Typed error mapping. Every failure leaves as ``ErrorResponse``.

Nothing here echoes request input or an exception's text unless that text is a fixed,
reviewed message, and every message passes through the redactor. A failure with no
mapping becomes a generic ``internal`` error; its detail goes to the filtered log only.
"""

from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ghostlab.contracts import ErrorInfo
from ghostlab.orchestration import CampaignSettingsChanged
from ghostlab.orchestration.inference import LiveInferenceRefused
from ghostlab.policy import ApprovalDenied, PolicyDenied
from ghostlab.providers import OutboundRefused, ProviderConfigError, ProviderRequestError
from ghostlab.security.redaction import Redactor
from ghostlab.storage.artifacts import ArtifactTooLarge
from ghostlab.storage.interfaces import Conflict, NotFound, StorageError

from .models import ErrorResponse

MAX_REPORTED_PROBLEMS = 20
MAX_LOCATION_DEPTH = 4


@dataclass(eq=False)
class ApiError(Exception):
    status: int
    code: str
    message: str
    reason: str | None = None
    retryable: bool = False
    headers: dict = field(default_factory=dict)


def not_found() -> ApiError:
    """The same for a missing id and another project's: nothing is disclosed."""
    return ApiError(404, "not_found", "Not found")


_OUTBOUND = {
    # reason: (status, code, retryable, fixed message). Nothing here echoes input.
    "retention_not_authorized": (
        403, "forbidden", False, "This server is not authorized to retain prompts at the provider"),
    "prohibited_content": (
        403, "forbidden", False, "The outbound request was refused; nothing was sent"),
    "payload_too_large": (
        413, "validation_failed", False, "The outbound request exceeds the size bound; nothing was sent"),
    "concurrency_exhausted": (
        429, "rate_limited", True, "All live inference slots are busy; retry later"),
}


_APPROVAL_DENIALS = {
    "approver_not_allowed": (403, "forbidden"),
    "approver_not_authorized": (403, "forbidden"),
    "capability_denied": (403, "forbidden"),
    "not_found": (404, "not_found"),
}


def _translate(error: Exception) -> ApiError | None:
    """The API form of a domain failure, or ``None`` if it has no mapping."""
    if isinstance(error, ApiError):
        return error
    if isinstance(error, PolicyDenied):
        decision = error.decision
        if decision.code == "cross_project":
            return not_found()
        info = decision.error_info()
        return ApiError(422 if info.code == "validation_failed" else 403, info.code,
                        decision.reason, reason=decision.code)
    if isinstance(error, ApprovalDenied):
        status, code = _APPROVAL_DENIALS.get(error.code, (409, "approval_invalid"))
        return ApiError(status, code, str(error), reason=error.code)
    if isinstance(error, CampaignSettingsChanged):
        return ApiError(409, "conflict", str(error), reason="settings_changed")
    if isinstance(error, (LiveInferenceRefused, ProviderConfigError)):
        return ApiError(403, "forbidden", "Inference is not authorized on this server",
                        reason="inference_not_authorized")
    if isinstance(error, OutboundRefused):
        status, code, retryable, message = _OUTBOUND[error.reason]
        return ApiError(status, code, message, reason=error.reason, retryable=retryable)
    if isinstance(error, ProviderRequestError):
        return ApiError(422, "validation_failed", "The model request was malformed; nothing was sent",
                        reason="malformed_model_request")
    if isinstance(error, NotFound):
        return not_found()
    if isinstance(error, ArtifactTooLarge):
        return ApiError(413, "validation_failed", "Artifact exceeds the read limit",
                        reason="too_large")
    if isinstance(error, Conflict):
        return ApiError(409, "conflict", "Conflicts with the stored record")
    if isinstance(error, StorageError):
        return ApiError(500, "internal", "Internal error", retryable=True)
    return None


def describe(error: Exception, redactor: Redactor) -> tuple[int, ErrorResponse, dict]:
    known = _translate(error) or ApiError(500, "internal", "Internal error")
    info = ErrorInfo(code=known.code, message=redactor.text(known.message)[:2000],
                     retryable=known.retryable,
                     side_effect="unknown" if known.code == "internal" else "none", ref=None)
    return known.status, ErrorResponse(error=info, reason=known.reason), known.headers


def respond(error: Exception, redactor: Redactor) -> JSONResponse:
    status, body, headers = describe(error, redactor)
    return JSONResponse(body.model_dump(mode="json"), status_code=status, headers=headers)


def _location(item: dict) -> str:
    """Declared fields only: an unknown field's name is input, so it is not repeated."""
    parts = item["loc"][:-1] if item["type"] == "extra_forbidden" else item["loc"]
    return ".".join(str(part) for part in parts[:MAX_LOCATION_DEPTH])


def _validation_message(error: RequestValidationError) -> str:
    """Where and how a request failed; the default handler would echo the input."""
    problems = [f"{_location(item)}: {item['type']}"
                for item in error.errors()[:MAX_REPORTED_PROBLEMS]]
    return "Invalid request: " + "; ".join(problems)


def install(app: FastAPI, redactor: Redactor) -> None:
    async def handle(request: Request, error: Exception):
        return respond(error, redactor)

    async def invalid(request: Request, error: RequestValidationError):
        return respond(ApiError(422, "validation_failed", _validation_message(error),
                                reason="invalid_request"), redactor)

    async def http_error(request: Request, error: HTTPException):
        if error.status_code == 404:
            return respond(not_found(), redactor)
        if error.status_code == 400:  # FastAPI's answer to a body it couldn't read as JSON
            return respond(ApiError(400, "validation_failed", "Request body could not be parsed",
                                    reason="unparsable_body"), redactor)
        code = "validation_failed" if error.status_code < 500 else "internal"
        return respond(ApiError(error.status_code, code, "Request not accepted"), redactor)

    app.add_exception_handler(RequestValidationError, invalid)
    app.add_exception_handler(HTTPException, http_error)
    for kind in (ApiError, PolicyDenied, ApprovalDenied, CampaignSettingsChanged,
                 LiveInferenceRefused, ProviderConfigError, OutboundRefused, ProviderRequestError,
                 NotFound, ArtifactTooLarge, Conflict, StorageError):
        app.add_exception_handler(kind, handle)
