"""Data-only package intake: validate and extract archives safely.

``validate_archive`` rejects traversal, absolute paths, unsafe links, and
expansion bombs WITHOUT fully decompressing: tar.gz members are scanned as
streaming headers and zip members via the central directory, with file
count and declared-size budgets enforced before any byte is written.
``extract_archive`` writes only into an existing backend-created workspace
whose realpath is re-verified per member. Archives are data, never code:
nothing here imports or executes payload content.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from .interface import BackendError

ARCHIVE_KINDS = ("tar.gz", "zip")

DEFAULT_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_FILES = 1024
DEFAULT_MAX_RATIO = 64

_CHUNK = 64 * 1024


def _check_caps(max_bytes: Any, max_files: Any, max_ratio: Any) -> None:
    for name, value, low in (("max_bytes", max_bytes, 1),
                             ("max_files", max_files, 1),
                             ("max_ratio", max_ratio, 1)):
        if type(value) is not int or value < low:
            raise ValueError(f"{name} must be an integer >= {low}")


def _reject(reason: str) -> BackendError:
    return BackendError(f"archive rejected: {reason}")


def _safe_name(name: Any) -> str:
    if type(name) is not str or not name or "\x00" in name:
        raise _reject("empty or non-text member name")
    if "\\" in name:
        raise _reject(f"backslash in member name '{name}'")
    stripped = name.strip()
    if not stripped or stripped.startswith("/") or stripped.startswith("\\\\"):
        raise _reject(f"absolute member path '{name}'")
    if len(stripped) >= 2 and stripped[1] == ":":
        raise _reject(f"absolute member path '{name}'")
    stripped = stripped.rstrip("/")
    if not stripped:
        raise _reject(f"empty member path '{name}'")
    parts = stripped.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _reject(f"traversal in member path '{name}'")
    if len(name) > 512:
        raise _reject("member name too long")
    return stripped


def _scan_tar(data: bytes, max_bytes: int, max_files: int,
              max_ratio: int) -> tuple[list, int]:
    budget = max_bytes * max_ratio
    files: list = []
    total = 0
    try:
        stream = io.BytesIO(data)
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for member in archive:
                if len(files) >= max_files and not member.isdir():
                    raise _reject(f"more than {max_files} files")
                clean = _safe_name(member.name)
                if member.issym() or member.islnk():
                    raise _reject(f"unsafe link '{clean}'")
                if member.isdir():
                    continue
                if not member.isreg():
                    raise _reject(f"non-regular member '{clean}'")
                if member.size < 0 or member.size > budget:
                    raise _reject(f"member '{clean}' exceeds expansion budget")
                total += member.size
                if total > budget:
                    raise _reject("total size exceeds expansion budget")
                files.append({"path": clean, "size": member.size})
    except BackendError:
        raise
    except (tarfile.TarError, EOFError, OSError) as error:
        raise _reject(f"unreadable tar.gz ({error})") from error
    return files, total


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _scan_zip(data: bytes, max_bytes: int, max_files: int,
              max_ratio: int) -> tuple[list, int]:
    budget = max_bytes * max_ratio
    files: list = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                name = info.filename
                if not name or name.endswith("/"):
                    if name and name != "/":
                        _safe_name(name.rstrip("/"))
                    continue
                clean = _safe_name(name)
                if len(files) >= max_files:
                    raise _reject(f"more than {max_files} files")
                if _is_zip_symlink(info):
                    raise _reject(f"unsafe link '{clean}'")
                if info.flag_bits & 0x1:
                    raise _reject(f"encrypted member '{clean}'")
                size = info.file_size
                if size < 0 or size > budget:
                    raise _reject(f"member '{clean}' exceeds expansion budget")
                total += size
                if total > budget:
                    raise _reject("total size exceeds expansion budget")
                files.append({"path": clean, "size": size})
    except BackendError:
        raise
    except (zipfile.BadZipFile, EOFError, OSError) as error:
        raise _reject(f"unreadable zip ({error})") from error
    return files, total


def validate_archive(data: Any, kind: str, max_bytes: int, max_files: int,
                     max_ratio: int) -> dict:
    """Validate archive headers against budgets; return its manifest.

    Raises BackendError on traversal, absolute paths, unsafe links,
    encryption, unreadable data, or budget overruns. Never extracts and
    never executes payload content.
    """
    if type(data) is not bytes or not data:
        raise ValueError("data must be non-empty bytes")
    if kind not in ARCHIVE_KINDS:
        raise ValueError(f"kind must be one of {ARCHIVE_KINDS}")
    _check_caps(max_bytes, max_files, max_ratio)
    if len(data) > max_bytes:
        raise _reject(f"compressed size {len(data)} exceeds {max_bytes}")
    if kind == "tar.gz":
        files, total = _scan_tar(data, max_bytes, max_files, max_ratio)
    else:
        files, total = _scan_zip(data, max_bytes, max_files, max_ratio)
    if not files:
        raise _reject("archive holds no files")
    return {
        "identity": hashlib.sha256(data).hexdigest(),
        "kind": kind,
        "files": files,
        "total_size": total,
        "compressed_size": len(data),
    }


def _workspace_base(dest_dir: Any) -> str:
    if isinstance(dest_dir, Path):
        dest_dir = str(dest_dir)
    if type(dest_dir) is not str or not dest_dir:
        raise ValueError("dest_dir must be a path string")
    if not os.path.isdir(dest_dir):
        raise BackendError("extract target must be an existing workspace")
    base = os.path.realpath(dest_dir)
    if not os.path.isdir(base):
        raise BackendError("extract target must be an existing workspace")
    return base


def _member_target(base: str, name: str) -> str:
    clean = _safe_name(name)
    target = os.path.realpath(os.path.join(base, clean))
    if target != base and not target.startswith(base + os.sep):
        raise _reject(f"member '{clean}' escapes the workspace")
    if target == base:
        raise _reject(f"member '{clean}' escapes the workspace")
    return target


def _copy_budgeted(source, target_path: str, budget: int) -> int:
    written = 0
    with open(target_path, "wb") as handle:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                return written
            written += len(chunk)
            if written > budget:
                handle.close()
                try:
                    os.unlink(target_path)
                except OSError:
                    pass
                raise _reject("extracted bytes exceed expansion budget")
            handle.write(chunk)


def extract_archive(data: bytes, kind: str, dest_dir: Any, *,
                    max_bytes: int, max_files: int,
                    max_ratio: int) -> dict:
    """Validate then extract into a backend-created workspace only.

    ``dest_dir`` must already exist as a real directory (the backend creates
    per-job workspaces); every member target is realpath-verified to stay
    inside it, and extracted bytes are re-budgeted during the copy as
    defense in depth. Returns the validated manifest.
    """
    base = _workspace_base(dest_dir)
    manifest = validate_archive(data, kind, max_bytes, max_files, max_ratio)
    budget = max_bytes * max_ratio
    remaining = [budget]
    if kind == "tar.gz":
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if member.isdir():
                        target = _member_target(base, member.name)
                        os.makedirs(target, exist_ok=True)
                        continue
                    # validate_archive already rejected links/specials;
                    # re-check here so extraction never trusts the rescan.
                    clean = _safe_name(member.name)
                    if not member.isreg():
                        raise _reject(f"non-regular member '{clean}'")
                    target = _member_target(base, member.name)
                    os.makedirs(os.path.dirname(target) or base, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise _reject(f"unreadable member '{clean}'")
                    with source:
                        remaining[0] -= _copy_budgeted(
                            source, target, remaining[0])
        except BackendError:
            raise
        except (tarfile.TarError, EOFError, OSError) as error:
            raise _reject(f"unreadable tar.gz ({error})") from error
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    name = info.filename
                    if not name or name.endswith("/"):
                        if name and name != "/":
                            os.makedirs(_member_target(base, name.rstrip("/")),
                                        exist_ok=True)
                        continue
                    target = _member_target(base, name)
                    os.makedirs(os.path.dirname(target) or base, exist_ok=True)
                    with archive.open(info, "r") as source:
                        remaining[0] -= _copy_budgeted(
                            source, target, remaining[0])
        except BackendError:
            raise
        except (zipfile.BadZipFile, EOFError, OSError) as error:
            raise _reject(f"unreadable zip ({error})") from error
    return manifest
