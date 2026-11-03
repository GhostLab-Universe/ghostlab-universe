"""Read-only repository inspection behind the existing session boundary."""
import asyncio
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from .github_source import GitHubInspector, GitHubImportError

from .account_models import SourceInspection, ACCOUNT_ERRORS, account_error

router = APIRouter(responses=ACCOUNT_ERRORS)
_slots = asyncio.Semaphore(2)

class SourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repository_url: str = Field(max_length=300)
    ref: str = Field(default="HEAD", max_length=200)

@router.post("/v1/sources/github/inspect", response_model=SourceInspection, operation_id="inspectGitHubSource")
async def inspect_source(body: SourceRequest):
    if _slots.locked():
        return account_error("Repository inspection is busy. Try again shortly.", 429)
    async with _slots:
        try:
            return await GitHubInspector().inspect(body.repository_url, body.ref)
        except GitHubImportError as error:
            return account_error(str(error), error.status)
