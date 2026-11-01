"""What evidence claims about its origin, and how much of that can be checked.

``run.json`` records the commit, the location of the imported package, a digest of the
code that actually ran, and a seal: one digest over the evidence files and those fields. A
release recomputes the code digest from the candidate commit's git objects, so code on
another path can't report a clean commit, and the gate recomputes the seal, so editing a
file or a field of ``run.json`` is caught.

The seal is an accident check, not an attestation. Nothing here is signed, so anyone who
can write these files can run the same function and write a matching seal. That is why
every record says ``origin_verified: false``.
"""

import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path

import ghostlab

ROOT = Path(__file__).resolve().parent.parent
# Everything that decides behaviour: path in the repository, and the suffix that counts.
CODE = (("src/ghostlab", ".py"), ("evals", ".py"), ("skills", ".json"), ("prompts", ".md"),
        ("migrations", ".sql"))
# What the seal covers in run.json, beside the files. Its own digest can't cover itself.
SEALED_FIELDS = ("commit", "dirty", "package_path", "code_sha256", "origin_verified")


def _digest(files: dict[str, bytes]) -> str:
    """Line endings are normalized, so a checkout's ``core.autocrlf`` doesn't matter."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0")
        digest.update(files[name].replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def code_sha256() -> str:
    """A digest of the code this process imported, wherever it was imported from."""
    package = Path(ghostlab.__file__).resolve().parent
    root = package.parent.parent
    places = {"src/ghostlab": package, "evals": ROOT / "evals", "skills": root / "skills",
              "prompts": root / "prompts", "migrations": root / "migrations"}
    files = {}
    for prefix, suffix in CODE:
        for path in places[prefix].rglob(f"*{suffix}"):
            relative = path.relative_to(places[prefix]).as_posix()
            files[f"{prefix}/{relative}"] = path.read_bytes()
    return _digest(files)


def _git(repo: Path, *args, data: bytes | None = None) -> bytes | None:
    done = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                          check=False)
    return done.stdout if done.returncode == 0 else None


@lru_cache(maxsize=32)
def code_sha256_at(repo: Path, commit: str) -> str | None:
    """The same digest, computed from a commit's objects. None if there's no such commit."""
    listed = _git(repo, "ls-tree", "-r", "--full-tree", "--name-only", commit, "--",
                  *(prefix for prefix, _ in CODE))
    if listed is None:
        return None
    names = [name for name in listed.decode().splitlines()
             if any(name.startswith(f"{prefix}/") and name.endswith(suffix)
                    for prefix, suffix in CODE)]
    batch = _git(repo, "cat-file", "--batch",
                 data="".join(f"{commit}:{name}\n" for name in names).encode())
    if batch is None:
        return None
    files, position = {}, 0
    for name in names:
        end = batch.index(b"\n", position)
        size = int(batch[position:end].split()[2])
        files[name] = batch[end + 1:end + 1 + size]
        position = end + 2 + size
    return _digest(files)


def file_at(repo: Path, commit: str, path: str) -> bytes | None:
    """A file as the commit holds it, whatever the working tree says."""
    found = _git(repo, "cat-file", "--batch", data=f"{commit}:{path}\n".encode())
    if found is None:
        return None
    header, _, body = found.partition(b"\n")
    fields = header.split()
    return body[:int(fields[2])] if len(fields) == 3 and fields[1] == b"blob" else None


def commit_exists(repo: Path, commit: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{commit}^{{commit}}") is not None


def evidence_sha256(directory: Path, identity: dict) -> str:
    """The seal: the evidence and telemetry files as stored, and who ``run.json`` says ran."""
    files = {"run.json": json.dumps({name: identity.get(name) for name in SEALED_FIELDS},
                                    sort_keys=True).encode()}
    for part in ("evidence", "telemetry"):
        for path in (Path(directory) / part).rglob("*"):
            if path.is_file():
                files[path.relative_to(directory).as_posix()] = path.read_bytes()
    return _digest(files)


def run_identity() -> dict:
    """The checkout this process is in, and the code it really imported."""
    head = _git(ROOT, "rev-parse", "HEAD")
    status = _git(ROOT, "status", "--porcelain")
    return {"commit": head.decode().strip() if head else None,
            "dirty": status is None or status.strip() != b"",
            "package_path": str(Path(ghostlab.__file__).resolve().parent),
            "code_sha256": code_sha256(), "origin_verified": False}
