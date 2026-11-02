"""Developer-owned agent registration with version+config capture.

Stdlib-only. Supports in-memory use and an optional track-private SQLite
file (tests pass a temp path; never a shared live DB).
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_STR = 100
_MAX_CONFIG_BYTES = 100_000


@dataclass(frozen=True)
class AgentManifest:
    """Developer-supplied declaration. Frozen; config deep-copied on use."""

    name: str
    version: str
    config: dict[str, Any]
    allowed_tools: tuple[str, ...]
    entry: str = ""
    description: str = ""

    def __post_init__(self):
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise ValueError("Invalid agent name")
        if not isinstance(self.version, str) or not self.version or len(self.version) > 64:
            raise ValueError("Invalid agent version")
        if not isinstance(self.config, dict):
            raise ValueError("Agent config must be an object")
        for key in self.config:
            if not isinstance(key, str) or not key or len(key) > _MAX_STR:
                raise ValueError("Invalid config key")
        try:
            encoded = json.dumps(self.config, sort_keys=True, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Agent config must be JSON-serializable: {error}") from error
        if len(encoded.encode()) > _MAX_CONFIG_BYTES:
            raise ValueError("Agent config too large")
        if not isinstance(self.allowed_tools, (tuple, list)) or not self.allowed_tools:
            raise ValueError("allowed_tools must be a non-empty list")
        for tool in self.allowed_tools:
            if not isinstance(tool, str) or not tool or len(tool) > _MAX_STR:
                raise ValueError("Invalid allowed tool name")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools must be unique")
        for field in (self.entry, self.description):
            if not isinstance(field, str) or len(field) > 500:
                raise ValueError("Invalid manifest string field")


@dataclass(frozen=True)
class AgentRecord:
    name: str
    version: str
    config: dict[str, Any]
    allowed_tools: tuple[str, ...]
    entry: str
    description: str
    registered_at: str
    source: str  # "registered" or "imported"


def _manifest_from_dict(data: dict[str, Any]) -> AgentManifest:
    if not isinstance(data, dict):
        raise ValueError("Manifest must be an object")
    allowed = data.get("allowed_tools")
    return AgentManifest(
        name=data.get("name", ""),
        version=data.get("version", ""),
        config=copy.deepcopy(data.get("config", {})),
        allowed_tools=tuple(allowed) if isinstance(allowed, (list, tuple)) else (),
        entry=data.get("entry", ""),
        description=data.get("description", ""),
    )


class TargetRegistry:
    """Owns agent identity, version and config snapshots."""

    def __init__(self, db_path: Path | str | None = None):
        self._agents: dict[str, AgentRecord] = {}
        self._db_path = Path(db_path) if db_path is not None else None
        if self._db_path is not None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS agents"
                    " (name TEXT PRIMARY KEY, manifest_json TEXT NOT NULL,"
                    " registered_at TEXT NOT NULL, source TEXT NOT NULL)"
                )
                conn.commit()
            finally:
                conn.close()
            self._load_from_db()

    def _connect(self):
        assert self._db_path is not None
        return sqlite3.connect(self._db_path)

    def _load_from_db(self):
        assert self._db_path is not None
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT name, manifest_json, registered_at, source FROM agents"
            ).fetchall()
        finally:
            conn.close()
        for name, manifest_json, registered_at, source in rows:
            data = json.loads(manifest_json)
            manifest = _manifest_from_dict(data)
            self._agents[name] = AgentRecord(
                name=manifest.name,
                version=manifest.version,
                config=copy.deepcopy(manifest.config),
                allowed_tools=tuple(manifest.allowed_tools),
                entry=manifest.entry,
                description=manifest.description,
                registered_at=registered_at,
                source=source,
            )

    def _persist(self, record: AgentRecord):
        if self._db_path is None:
            return
        manifest_json = json.dumps(
            {
                "name": record.name,
                "version": record.version,
                "config": record.config,
                "allowed_tools": list(record.allowed_tools),
                "entry": record.entry,
                "description": record.description,
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO agents (name, manifest_json, registered_at, source)"
                " VALUES (?, ?, ?, ?)",
                (record.name, manifest_json, record.registered_at, record.source),
            )
            conn.commit()
        finally:
            conn.close()

    def register(self, manifest: AgentManifest | dict[str, Any]) -> AgentRecord:
        if isinstance(manifest, dict):
            manifest = _manifest_from_dict(copy.deepcopy(manifest))
        if not isinstance(manifest, AgentManifest):
            raise ValueError("register() requires an AgentManifest or dict")
        if manifest.name in self._agents:
            raise ValueError(f"Agent already registered: {manifest.name}")
        record = AgentRecord(
            name=manifest.name,
            version=manifest.version,
            config=copy.deepcopy(manifest.config),
            allowed_tools=tuple(manifest.allowed_tools),
            entry=manifest.entry,
            description=manifest.description,
            registered_at=datetime.now(timezone.utc).isoformat(),
            source="registered",
        )
        self._agents[record.name] = record
        self._persist(record)
        return copy.deepcopy(record)

    def import_manifest(self, data: dict[str, Any] | Path | str) -> AgentRecord:
        """Import a JSON manifest dict or a path to a JSON manifest file."""
        if isinstance(data, (Path, str)) and not isinstance(data, dict):
            path = Path(data)
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = copy.deepcopy(data)
        if not isinstance(raw, dict):
            raise ValueError("Manifest import must be an object")
        manifest = _manifest_from_dict(raw)
        if manifest.name in self._agents:
            raise ValueError(f"Agent already registered: {manifest.name}")
        record = AgentRecord(
            name=manifest.name,
            version=manifest.version,
            config=copy.deepcopy(manifest.config),
            allowed_tools=tuple(manifest.allowed_tools),
            entry=manifest.entry,
            description=manifest.description,
            registered_at=datetime.now(timezone.utc).isoformat(),
            source="imported",
        )
        self._agents[record.name] = record
        self._persist(record)
        return copy.deepcopy(record)

    def export_manifest(self, name: str) -> dict[str, Any]:
        record = self.get(name)
        if record is None:
            raise ValueError(f"Unknown agent: {name}")
        return {
            "name": record.name,
            "version": record.version,
            "config": copy.deepcopy(record.config),
            "allowed_tools": list(record.allowed_tools),
            "entry": record.entry,
            "description": record.description,
        }

    def get(self, name: str) -> AgentRecord | None:
        record = self._agents.get(name)
        return copy.deepcopy(record) if record is not None else None

    def list(self) -> list[AgentRecord]:
        return [copy.deepcopy(record) for _, record in sorted(self._agents.items())]
