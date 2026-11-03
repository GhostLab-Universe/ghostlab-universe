"""Explicit registration of reviewed local adapters (SQLite, serial local).

Installing code and importing configuration stay separate: the operator
installs and registers reviewed adapter code with an explicit module
allowlist, and the application imports JSON manifests that reference an
existing registration. Importing JSON never installs packages, executes
code or fetches remote references.

Each developer keeps a local database file; live database files are never
committed or shared. Tests pass a temporary path, never the default.
Execution starts through :func:`resolve_class`, which refuses unknown,
code-changed or unqualified environments.
"""

import hashlib
import inspect
import importlib
import json
import re
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .manifest import (ENV_ID_PATTERN, ENV_VERSION_PATTERN, MODE_PATTERN,
                       TOOL_NAME_PATTERN, WORLD_PATTERN, canonical_digest,
                       parse_manifest_bytes, validate_manifest)

MODULE_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]{0,199}$")
CLASS_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,99}$")
REQUIRED_METHODS = ("describe", "named_fixture", "reset", "execute", "observe",
                    "faults_fired", "evaluate", "close")
DEFAULT_ALLOWED_PREFIXES = ("ghostlab.",)
QUALIFIER_VERSION = "ghostlab-qualify/1"

STATUS_NOT_QUALIFIED = "not qualified"
STATUS_QUALIFIED = "qualified for declared capabilities"
STATUS_FAILED = "qualification failed"
STATUSES = (STATUS_NOT_QUALIFIED, STATUS_QUALIFIED, STATUS_FAILED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations (
  world_version TEXT PRIMARY KEY,
  environment_id TEXT NOT NULL,
  environment_version TEXT NOT NULL,
  adapter_ref TEXT NOT NULL,
  display_name TEXT NOT NULL,
  declared_tools TEXT NOT NULL,
  declared_modes TEXT NOT NULL,
  code_digest TEXT NOT NULL,
  registered_by TEXT NOT NULL,
  registered_at TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
  manifest_digest TEXT PRIMARY KEY,
  world_version TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qual_reports (
  report_id INTEGER PRIMARY KEY AUTOINCREMENT,
  world_version TEXT NOT NULL,
  environment_version TEXT NOT NULL,
  adapter_ref TEXT NOT NULL,
  code_digest TEXT NOT NULL,
  manifest_digest TEXT,
  runner_version TEXT NOT NULL,
  status TEXT NOT NULL,
  checks_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_adapter_ref(adapter_ref: str) -> tuple:
    if type(adapter_ref) is not str or adapter_ref.count(":") != 1:
        raise ValueError("adapter_ref must look like 'module:Class'")
    module_name, class_name = adapter_ref.split(":")
    if not MODULE_PATTERN.match(module_name) or not CLASS_PATTERN.match(class_name):
        raise ValueError("adapter_ref must look like 'module:Class'")
    return module_name, class_name


def check_adapter_ref_allowed(adapter_ref: str, allowed_prefixes) -> None:
    if (not isinstance(allowed_prefixes, (list, tuple)) or not allowed_prefixes
            or any(type(prefix) is not str or not prefix
                   for prefix in allowed_prefixes)):
        raise ValueError("allowed prefixes must be a non-empty string list")
    module_name, _ = split_adapter_ref(adapter_ref)
    for prefix in allowed_prefixes:
        if module_name == prefix or module_name.startswith(
                prefix if prefix.endswith(".") else prefix + "."):
            return
    raise ValueError(f"adapter module '{module_name}' is not in the allowlist")


def resolve_adapter_ref(adapter_ref: str, allowed_prefixes) -> type:
    """Import and return the adapter class without instantiating it."""
    check_adapter_ref_allowed(adapter_ref, allowed_prefixes)
    module_name, class_name = split_adapter_ref(adapter_ref)
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise ValueError(f"adapter module '{module_name}' cannot be imported") from error
    cls = getattr(module, class_name, None)
    if not isinstance(cls, type):
        raise ValueError(f"adapter '{adapter_ref}' is not a class")
    missing = [name for name in REQUIRED_METHODS if not callable(getattr(cls, name, None))]
    if missing:
        raise ValueError(f"adapter '{adapter_ref}' misses: {', '.join(missing)}")
    return cls


def adapter_code_digest(cls: type) -> str:
    """Bind a registration to the reviewed source file behind the class."""
    try:
        source_file = inspect.getsourcefile(cls)
    except (OSError, TypeError) as error:
        raise ValueError(f"adapter '{cls.__name__}' has no readable source") from error
    if not source_file:
        raise ValueError(f"adapter '{cls.__name__}' has no readable source")
    with open(source_file, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _check_tools(declared_tools) -> list:
    if type(declared_tools) is not list or not 1 <= len(declared_tools) <= 32:
        raise ValueError("declared_tools must list 1-32 tools")
    seen = set()
    for entry in declared_tools:
        if type(entry) is not dict or set(entry) != {"name", "parameters"}:
            raise ValueError("each declared tool needs exactly name and parameters")
        name, parameters = entry["name"], entry["parameters"]
        if (type(name) is not str or not TOOL_NAME_PATTERN.match(name)
                or name in seen):
            raise ValueError(f"invalid or duplicate tool name '{name}'")
        seen.add(name)
        if (type(parameters) is not list or len(parameters) > 16
                or any(type(p) is not str or not p for p in parameters)
                or len(set(parameters)) != len(parameters)):
            raise ValueError(f"invalid parameters for tool '{name}'")
    return deepcopy(declared_tools)


def _check_modes(declared_modes) -> list:
    if type(declared_modes) is not list or not 1 <= len(declared_modes) <= 8:
        raise ValueError("declared_modes must list 1-8 reproduction modes")
    if (any(type(m) is not str or not MODE_PATTERN.match(m) for m in declared_modes)
            or len(set(declared_modes)) != len(declared_modes)):
        raise ValueError("invalid or duplicate reproduction mode")
    return list(declared_modes)


class OperatorRegistry:
    """SQLite-backed operator registrations, manifests and qual reports."""

    def __init__(self, path, *, allowed_adapter_prefixes=DEFAULT_ALLOWED_PREFIXES):
        self._path = Path(path)
        if (not isinstance(allowed_adapter_prefixes, (list, tuple))
                or not allowed_adapter_prefixes):
            raise ValueError("allowed prefixes must be a non-empty list")
        for prefix in allowed_adapter_prefixes:
            bare = prefix[:-1] if prefix.endswith(".") else prefix
            if type(prefix) is not str or not MODULE_PATTERN.match(bare):
                raise ValueError(f"invalid allowed prefix {prefix!r}")
        self._prefixes = tuple(allowed_adapter_prefixes)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self._path))
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @property
    def allowed_prefixes(self):
        return self._prefixes

    def register(self, *, world_version, environment_id, environment_version,
                 adapter_ref, display_name, declared_tools, declared_modes,
                 registered_by):
        """Register reviewed adapter code. Imports it once to check the shape."""
        if type(world_version) is not str or not WORLD_PATTERN.match(world_version):
            raise ValueError("invalid world_version")
        if type(environment_id) is not str or not ENV_ID_PATTERN.match(environment_id):
            raise ValueError("invalid environment_id")
        if (type(environment_version) is not str
                or not ENV_VERSION_PATTERN.match(environment_version)):
            raise ValueError("invalid environment_version")
        if type(display_name) is not str or not display_name or len(display_name) > 200:
            raise ValueError("invalid display_name")
        if type(registered_by) is not str or not registered_by:
            raise ValueError("invalid registered_by")
        tools = _check_tools(declared_tools)
        modes = _check_modes(declared_modes)
        cls = resolve_adapter_ref(adapter_ref, self._prefixes)
        digest = adapter_code_digest(cls)
        record = {"world_version": world_version, "environment_id": environment_id,
                  "environment_version": environment_version, "adapter_ref": adapter_ref,
                  "display_name": display_name, "declared_tools": tools,
                  "declared_modes": modes, "code_digest": digest,
                  "registered_by": registered_by, "registered_at": utcnow(),
                  "status": STATUS_NOT_QUALIFIED}
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO registrations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (world_version, environment_id, environment_version, adapter_ref,
                     display_name, json.dumps(tools), json.dumps(modes), digest,
                     registered_by, record["registered_at"], STATUS_NOT_QUALIFIED))
            except sqlite3.IntegrityError as error:
                raise ValueError(f"'{world_version}' is already registered") from error
        return deepcopy(record)

    def get(self, world_version):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM registrations WHERE world_version = ?",
                (world_version,)).fetchone()
        if row is None:
            raise LookupError(f"unknown environment '{world_version}'")
        return {"world_version": row[0], "environment_id": row[1],
                "environment_version": row[2], "adapter_ref": row[3],
                "display_name": row[4], "declared_tools": json.loads(row[5]),
                "declared_modes": json.loads(row[6]), "code_digest": row[7],
                "registered_by": row[8], "registered_at": row[9], "status": row[10]}

    def get_manifest(self, manifest_digest):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM manifests WHERE manifest_digest = ?",
                (manifest_digest,)).fetchone()
        if row is None:
            raise LookupError("unknown manifest digest")
        return json.loads(row[0])

    def list_envs(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT world_version, environment_id, environment_version,"
                " display_name, status FROM registrations"
                " ORDER BY world_version").fetchall()
        return [{"world_version": row[0], "environment_id": row[1],
                 "environment_version": row[2], "display_name": row[3],
                 "status": row[4]} for row in rows]

    def import_manifest(self, data: bytes):
        """Validate manifest bytes against an existing registration and pin them."""
        manifest = validate_manifest(parse_manifest_bytes(data))
        record = self.get(manifest["world_version"])
        if manifest["adapter_ref"] != record["adapter_ref"]:
            raise ValueError("manifest adapter_ref does not match the registration")
        digest = canonical_digest(manifest)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO manifests VALUES (?,?,?,?)",
                (digest, manifest["world_version"], json.dumps(manifest, sort_keys=True),
                 utcnow()))
        return {"manifest_digest": digest, "world_version": manifest["world_version"],
                "manifest": manifest}

    def record_qualification(self, *, world_version, checks, passed,
                             manifest_digest=None):
        """Persist one qualification outcome against exact versions and code."""
        record = self.get(world_version)
        if type(checks) is not list or not checks:
            raise ValueError("checks must be a non-empty list")
        if type(passed) is not bool:
            raise ValueError("passed must be boolean")
        if manifest_digest is not None and type(manifest_digest) is not str:
            raise ValueError("invalid manifest digest")
        status = STATUS_QUALIFIED if passed else STATUS_FAILED
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO qual_reports (world_version, environment_version,"
                " adapter_ref, code_digest, manifest_digest, runner_version, status,"
                " checks_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (world_version, record["environment_version"], record["adapter_ref"],
                 record["code_digest"], manifest_digest, QUALIFIER_VERSION, status,
                 json.dumps(checks), utcnow()))
            connection.execute("UPDATE registrations SET status = ? WHERE world_version = ?",
                               (status, world_version))
        return {"report_id": cursor.lastrowid, "status": status}

    def qualification_status(self, world_version):
        record = self.get(world_version)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, environment_version, code_digest, runner_version,"
                " created_at FROM qual_reports WHERE world_version = ?"
                " ORDER BY report_id DESC LIMIT 1", (world_version,)).fetchone()
        if row is None:
            return {"status": STATUS_NOT_QUALIFIED, "report": None}
        current = (row[1] == record["environment_version"]
                   and row[2] == record["code_digest"]
                   and row[3] == QUALIFIER_VERSION)
        return {"status": row[0] if current else STATUS_NOT_QUALIFIED,
                "report": {"status": row[0], "environment_version": row[1],
                           "code_digest": row[2], "runner_version": row[3],
                           "created_at": row[4], "current": current}}

    def describe_only(self, world_version):
        """Describe-only capability discovery: instantiate, describe, close.

        Refuses unknown registrations and adapter code that changed since
        review. Never resets, invokes, faults, closes a run, qualifies,
        or calls a model. Qualification is reported by
        qualification_status, not enforced here.
        """
        record = self.get(world_version)
        cls = resolve_adapter_ref(record["adapter_ref"], self._prefixes)
        if adapter_code_digest(cls) != record["code_digest"]: 
            raise ValueError(f"adapter code for "+world_version+" changed since review")
        adapter = cls()
        try:
            description = adapter.describe()
        finally:
            adapter.close()
        return {"world_version": description.world_version,
                "environment_id": description.environment_id,
                "environment_version": description.environment_version,
                "tools": [{"name": tool.name, "effect": tool.effect,
                           "parameters": dict(tool.parameters)}
                          for tool in description.tools],
                "faults": [{"id": fault.fault_id,
                            "parameters": [{"name": item.name, "type": item.type}
                                           for item in fault.parameters],
                            "trigger_points": list(fault.trigger_points)}
                           for fault in description.faults],
                "invariants": list(description.invariants),
                "fixtures": list(description.fixtures),
                "capabilities": dict(description.capabilities),
                "modes": list(record["declared_modes"])}

    def manifest_agreement(self, world_version, manifest_digest):
        """Manifest claims versus described reality: agreement plus detail.

        Compares the pinned manifest against a fresh describe-only
        discovery. Unknown digests raise LookupError; code-changed
        adapters raise ValueError. Callers refuse a False agreement
        instead of pinning claims the adapter disproves.
        """
        manifest = self.get_manifest(manifest_digest)
        if manifest["world_version"] != world_version:
            return False, "manifest names a different world"
        described = self.describe_only(world_version)
        mismatches = []
        if (sorted(tool["name"] for tool in manifest["tools"])
                != sorted(tool["name"] for tool in described["tools"])):
            mismatches.append("tools")
        for key in ("fixtures", "invariants"):
            if sorted(manifest[key]) != sorted(described[key]):
                mismatches.append(key)
        if (sorted(manifest["fault_hooks"])
                != sorted(fault["id"] for fault in described["faults"])):
            mismatches.append("fault_hooks")
        if dict(manifest["capabilities"]) != described["capabilities"]:
            mismatches.append("capabilities")
        if mismatches:
            return False, ("manifest disagrees on: "
                           + ", ".join(sorted(mismatches)))
        return True, "manifest matches the described adapter"

    def resolve_class(self, world_version):
        """Resolve the pinned adapter class, gated on health.

        Refuses unknown registrations, adapter code that changed since
        review, and environments whose latest qualification for these exact
        versions did not pass. Returns the class; registers nothing and
        instantiates nothing. The caller instantiates for describe-only
        discovery or gated execution.
        """
        record = self.get(world_version)
        cls = resolve_adapter_ref(record["adapter_ref"], self._prefixes)
        if adapter_code_digest(cls) != record["code_digest"]:
            raise ValueError(f"'{world_version}' adapter code changed since review;"
                             " re-register and re-qualify")
        qualified = self.qualification_status(world_version)
        if qualified["status"] != STATUS_QUALIFIED:
            raise ValueError(f"'{world_version}' is {qualified['status']};"
                             " qualify it before controlled execution")
        return cls
