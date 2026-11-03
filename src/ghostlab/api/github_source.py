"""Read-only, public GitHub source inspection. Never installs or executes code."""

import asyncio
import base64
import hashlib
import json
import re
from urllib.parse import quote, urlsplit

import httpx


SHA = re.compile(r"^[0-9a-f]{40}$")
PART = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}$")
MAX_RESPONSE = 2_000_000
MAX_CONFIG = 65_536
SETUP_FILES = {"pyproject.toml", "requirements.txt", "uv.lock", "package.json",
               "package-lock.json", "Dockerfile", "compose.yaml", "openapi.json",
               "openapi.yaml", "ghostlab.json"}


class GitHubImportError(ValueError):
    """Safe error message; upstream response bodies are never returned."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def parse_repository(url):
    if type(url) is not str or len(url) > 300 or any(ord(c) < 33 for c in url):
        raise GitHubImportError("Enter an HTTPS github.com/owner/repository URL")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.query or parsed.fragment:
        raise GitHubImportError("Only HTTPS github.com repository URLs are supported")
    parts = parsed.path.rstrip("/").split("/")
    if len(parts) != 3 or parts[0] != "":
        raise GitHubImportError("Use the repository URL, not a file or branch URL")
    owner, repo = parts[1:]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not all(PART.fullmatch(p) and p not in {".", ".."} for p in (owner, repo)):
        raise GitHubImportError("Invalid GitHub owner or repository")
    return owner, repo


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(data):
    try:
        value = json.loads(data, object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError, UnicodeError):
        raise GitHubImportError("Invalid or excessively nested source metadata", 422) from None
    if type(value) is not dict:
        raise GitHubImportError("Expected a JSON object", 422)
    return value


class GitHubInspector:
    """No credentials, proxies, redirects, archive downloads or model calls."""

    def __init__(self, transport=None):
        self.transport = transport

    async def inspect(self, repository_url, ref="HEAD"):
        owner, repo = parse_repository(repository_url)
        if type(ref) is not str or not 1 <= len(ref) <= 200 or not re.fullmatch(r"[A-Za-z0-9_./-]+", ref) or ".." in ref:
            raise GitHubImportError("Invalid branch, tag or commit reference")
        try:
            async with asyncio.timeout(20):
                async with httpx.AsyncClient(
                    transport=self.transport, trust_env=False, follow_redirects=False,
                    timeout=8, headers={"Accept": "application/vnd.github+json",
                                        "Accept-Encoding": "identity",
                                        "User-Agent": "GhostLab-source-inspector"},
                ) as client:
                    return await self._inspect(client, owner, repo, ref)
        except (TimeoutError, httpx.TimeoutException):
            raise GitHubImportError("GitHub inspection timed out; retry explicitly", 504) from None
        except httpx.HTTPError:
            raise GitHubImportError("Unable to contact GitHub", 502) from None

    async def _get(self, client, path):
        async with client.stream("GET", "https://api.github.com" + path) as response:
            if response.status_code != 200:
                status = response.status_code
                if status == 404:
                    raise GitHubImportError("Repository/ref unavailable. Public repositories only; private access is not configured", 404)
                if status in (403, 429):
                    raise GitHubImportError("GitHub refused or rate-limited inspection; retry later", 429)
                raise GitHubImportError("GitHub returned an unsupported response; redirects are refused", 502)
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise GitHubImportError("Compressed source metadata is refused", 502)
            data = bytearray()
            async for chunk in response.aiter_raw():
                if len(data) + len(chunk) > MAX_RESPONSE:
                    raise GitHubImportError("Repository metadata exceeds the inspection limit", 413)
                data.extend(chunk)
            return _json(data)

    async def _inspect(self, client, owner, repo, ref):
        root = f"/repos/{owner}/{repo}"
        commit = await self._get(client, root + "/commits/" + quote(ref, safe=""))
        sha = commit.get("sha")
        detail = commit.get("commit")
        tree_detail = detail.get("tree") if type(detail) is dict else None
        tree_sha = tree_detail.get("sha") if type(tree_detail) is dict else None
        if type(sha) is not str or not SHA.fullmatch(sha) or type(tree_sha) is not str or not SHA.fullmatch(tree_sha):
            raise GitHubImportError("GitHub did not provide a valid immutable commit/tree", 502)
        tree = await self._get(client, root + "/git/trees/" + tree_sha + "?recursive=1")
        entries = tree.get("tree")
        if tree.get("truncated") is not False or type(entries) is not list or len(entries) > 10_000:
            raise GitHubImportError("Repository is too large for complete inspection; no partial readiness claim is made", 413)
        files = {}
        skipped = 0
        for item in entries:
            if type(item) is not dict or type(item.get("path")) is not str:
                raise GitHubImportError("Malformed GitHub tree metadata", 502)
            path = item["path"]
            if path.startswith("/") or "\\" in path or any(p in {"", ".", ".."} for p in path.split("/")):
                raise GitHubImportError("Unsafe repository path", 422)
            if item.get("mode") in {"120000", "160000"}:
                skipped += 1
                continue
            if item.get("type") == "blob" and item.get("mode") in {"100644", "100755"} and path in SETUP_FILES:
                files[path] = item
        manifest = None
        manifest_status = "missing"
        config_sha256 = None
        if "ghostlab.json" in files:
            entry = files["ghostlab.json"]
            size, blob_sha = entry.get("size"), entry.get("sha")
            if type(size) is not int or not 0 <= size <= MAX_CONFIG or type(blob_sha) is not str or not SHA.fullmatch(blob_sha):
                raise GitHubImportError("ghostlab.json exceeds the limit or has invalid metadata", 413)
            blob = await self._get(client, root + "/git/blobs/" + blob_sha)
            try:
                if blob.get("encoding") != "base64" or type(blob.get("content")) is not str:
                    raise ValueError()
                raw = base64.b64decode("".join(blob["content"].split()), validate=True)
                if len(raw) != size or len(raw) > MAX_CONFIG or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != blob_sha:
                    raise ValueError()
            except (ValueError, TypeError):
                raise GitHubImportError("ghostlab.json integrity validation failed", 422) from None
            config_sha256 = hashlib.sha256(raw).hexdigest()
            _json(raw)
            # This API owns no environment-manifest schema. Do not claim validation
            # or return unreviewed source text to the browser.
            manifest_status = "needs-configuration-review"
        return {
            "schema_version": 1, "repository_url": f"https://github.com/{owner}/{repo}",
            "requested_ref": ref, "commit_sha": sha, "tree_sha": tree_sha,
            "setup_files": sorted(files), "manifest_status": manifest_status,
            "manifest_candidate": manifest, "config_sha256": config_sha256,
            "skipped_links_and_submodules": skipped, "status": "source-inspected-not-runnable",
            "next_steps": ["Review or create the environment manifest",
                           "Have the operator review/package the pinned code using a qualified execution backend",
                           "Register the adapter explicitly, then import its manifest",
                           "Qualify reset, isolation, evidence and fault hooks before experiments"],
            "limitations": ["Public repositories only; no GitHub credentials are read",
                            "Only root setup filenames and ghostlab.json are inspected",
                            "No code downloaded for execution, installed, built, registered or run",
                            "Source inspection is not environment qualification or sandbox approval"],
        }
