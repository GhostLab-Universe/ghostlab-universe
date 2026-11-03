"""Where sanitized records go. Sinks never see anything ``Telemetry`` didn't clean.

The file sink is a local evidence store: it's bounded, private to the user where the
operating system supports that, and readable without any hosted account.
"""

import json
import os
import threading
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from .schema import TelemetryRecord, validate_record

DEFAULT_MAX_EVENTS = 10_000
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_BACKUPS = 1


@runtime_checkable
class TelemetrySink(Protocol):
    def write(self, record: TelemetryRecord) -> None: ...

    def close(self) -> None: ...


class NullSink:
    def write(self, record: TelemetryRecord) -> None:
        pass

    def close(self) -> None:
        pass


class InMemorySink:
    """Keeps the newest ``max_events`` records. ``dropped`` counts what was evicted."""

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS):
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._records = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self.dropped = 0

    def write(self, record: TelemetryRecord) -> None:
        with self._lock:
            if len(self._records) == self._records.maxlen:
                self.dropped += 1
            self._records.append(record)

    def close(self) -> None:
        pass

    @property
    def records(self) -> tuple[TelemetryRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def dump(self) -> str:
        return "\n".join(json.dumps(record.to_json(), sort_keys=True) for record in self.records)


class FanOutSink:
    """Writes to every sink. One failing sink doesn't stop the others."""

    def __init__(self, *sinks: TelemetrySink):
        self.sinks, self.failures = sinks, 0

    def write(self, record: TelemetryRecord) -> None:
        for sink in self.sinks:
            try:
                sink.write(record)
            except Exception:  # noqa: BLE001 - telemetry never reaches the business path
                self.failures += 1

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:  # noqa: BLE001
                self.failures += 1


class FileSink:
    """JSON Lines with a hard size bound.

    The size is checked before each write, so no file ever exceeds ``max_bytes``, and the
    set of files never exceeds ``max_bytes * (backups + 1)``. When a write wouldn't fit,
    the file rotates and the oldest backup is deleted. With ``backups=0`` nothing rotates
    and later records are dropped instead. One record larger than ``max_bytes`` is always
    dropped. ``dropped`` counts both cases.

    One process writes a given path. Files are created owner-only on POSIX; on Windows
    they inherit the directory's ACL, so the directory must be private (see M09.md).
    """

    def __init__(self, path: Path | str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 backups: int = DEFAULT_BACKUPS):
        if max_bytes < 1 or backups < 0:
            raise ValueError("max_bytes must be positive and backups non-negative")
        self.path, self.max_bytes, self.backups = Path(path), max_bytes, backups
        self.dropped = 0
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("refusing a telemetry path that is a symbolic link")
        self._size = self.path.stat().st_size if self.path.exists() else 0

    def _backup(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _rotate(self) -> None:
        self._backup(self.backups).unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            if self._backup(index).exists():
                self._backup(index).replace(self._backup(index + 1))
        if self.path.exists():
            self.path.replace(self._backup(1))
        self._size = 0

    def write(self, record: TelemetryRecord) -> None:
        line = (json.dumps(record.to_json(), sort_keys=True, separators=(",", ":"))
                + "\n").encode()
        with self._lock:
            if len(line) > self.max_bytes:
                self.dropped += 1
                return
            if self._size + len(line) > self.max_bytes:
                if self.backups == 0:
                    self.dropped += 1
                    return
                self._rotate()
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(
                os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            try:
                os.write(descriptor, line)
            finally:
                os.close(descriptor)
            self._size += len(line)

    def close(self) -> None:
        pass


def sink_files(path: Path | str) -> list[Path]:
    """A sink's files, oldest first: ``name.N`` ... ``name.1``, ``name``."""
    path = Path(path)
    backups = sorted((p for p in path.parent.glob(f"{path.name}.*")
                      if p.name.rsplit(".", 1)[1].isdigit()),
                     key=lambda p: int(p.name.rsplit(".", 1)[1]), reverse=True)
    return backups + ([path] if path.exists() else [])


def read_records(path: Path | str) -> tuple[list[dict], list[str]]:
    """Stored records, oldest first, and the problems found. Nothing is trusted or fixed."""
    records, problems = [], []
    for item in sink_files(path):
        for number, line in enumerate(item.read_text("utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
            except ValueError:
                problems.append(f"{item.name}:{number}: not JSON")
                continue
            if found := validate_record(record):
                problems.append(f"{item.name}:{number}: {'; '.join(found)}")
            else:
                records.append(record)
    return records, problems


def prune_expired(path: Path | str, *, max_age: timedelta,
                  now: datetime | None = None) -> list[Path]:
    """Delete a sink's files untouched for longer than ``max_age``. Returns what went.

    A whole file goes at once, so a record can outlive ``max_age`` by as long as its
    file stayed active. Files under an audit hold must be moved out of the sink first.
    """
    now = now or datetime.now(timezone.utc)
    removed = []
    for item in sink_files(path):
        modified = datetime.fromtimestamp(item.stat().st_mtime, timezone.utc)
        if now - modified > max_age:
            item.unlink()
            removed.append(item)
    return removed


def record_lines(records: Iterable[Mapping]) -> str:
    """A stable text form of plain records, for scans and comparisons."""
    return "\n".join(json.dumps(dict(record), sort_keys=True) for record in records)
