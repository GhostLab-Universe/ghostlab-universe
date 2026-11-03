import asyncio
import json

import httpx
import pytest

from ghostlab.api.github_source import GitHubImportError, GitHubInspector, parse_repository


@pytest.mark.parametrize("url", ["http://github.com/a/b", "https://github.com.evil/a/b",
    "https://u@github.com/a/b", "https://github.com/a/b/tree/main", "https://github.com/a/b?token=x"])
def test_rejects_non_repository_urls(url):
    with pytest.raises(GitHubImportError):
        parse_repository(url)


def test_inspection_pins_tree_without_execution():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        assert "authorization" not in request.headers
        data = {"sha": "a"*40, "commit": {"tree": {"sha": "b"*40}}} if len(seen) == 1 else {
            "truncated": False, "tree": [{"path": "pyproject.toml", "mode": "100644", "type": "blob"}]}
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(data).encode()))
    report = asyncio.run(GitHubInspector(httpx.MockTransport(handler)).inspect("https://github.com/a/b", "main"))
    assert report["status"] == "source-inspected-not-runnable"
    assert report["commit_sha"] == "a"*40
    assert report["setup_files"] == ["pyproject.toml"]
    assert seen[1].endswith("/git/trees/" + "b"*40 + "?recursive=1")


@pytest.mark.parametrize("status", [302, 403, 404, 429, 500])
def test_upstream_refusals_do_not_follow_redirects_or_echo_body(status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.test/"}, content=b"secret-canary")
    with pytest.raises(GitHubImportError) as error:
        asyncio.run(GitHubInspector(httpx.MockTransport(handler)).inspect("https://github.com/a/b"))
    assert "secret-canary" not in str(error.value)
    assert len(calls) == 1
