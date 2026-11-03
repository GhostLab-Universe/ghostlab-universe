"""Additive account/source API responses; no changes to world contracts."""
from typing import Literal
from pydantic import BaseModel, ConfigDict
from .models import ErrorResponse
from fastapi.responses import JSONResponse

class AuthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    providers: list[Literal["github", "google"]]
    signed_in: bool

class LogoutResult(BaseModel):
    signed_out: bool

class SourceInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    repository_url: str
    requested_ref: str
    commit_sha: str
    tree_sha: str
    setup_files: list[str]
    manifest_status: Literal["missing", "needs-configuration-review"]
    manifest_candidate: None
    config_sha256: str | None
    skipped_links_and_submodules: int
    status: Literal["source-inspected-not-runnable"]
    next_steps: list[str]
    limitations: list[str]

ACCOUNT_ERRORS = {code: {"model": ErrorResponse, "description": "Request refused or provider unavailable"}
                  for code in (400, 401, 403, 404, 413, 422, 429, 500, 502, 503, 504)}

def account_error(message: str, status: int):
    code = "forbidden" if status == 403 else "validation_failed" if status in {400, 422} else "internal"
    return JSONResponse({"error": {"code": code, "message": message, "retryable": status in {429, 502, 503, 504},
                                   "side_effect": "none", "ref": None}, "reason": None}, status_code=status)
