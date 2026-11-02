"""Bounded JSON manifest import referencing an existing registration.

A manifest declares what an environment claims to offer; it carries no
code, no package installs, no URLs and no filesystem paths. Unknown fields
are rejected, so ``pip``/``code``/``url``/``path`` style keys can never
sneak in. Validation is pure: it never imports adapter code and never runs
anything. Claims are checked against the adapter at qualification time.
"""

import hashlib
import json
import os
import re

MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 262_144

_MANIFEST_KEYS = {"manifest_version", "world_version", "environment_id",
                  "environment_version", "adapter_ref", "display_name", "tools",
                  "fixtures", "fault_hooks", "invariants", "capabilities",
                  "reproduction_modes", "notes"}
_TOOL_KEYS = {"name", "parameters"}
WORLD_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}/[a-z0-9][a-z0-9._/-]{0,99}$")
ENV_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
ENV_VERSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,99}$")
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PARAM_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
MODE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")
_WINDOWS_ABS_PATTERN = re.compile(r"^[A-Za-z]:[\\\\/]|^\\\\\\\\")


def _forbidden_value(text: str) -> str | None:
    if _URL_PATTERN.search(text):
        return "URL"
    # os.path.isabs is platform-dependent; reject POSIX roots everywhere too.
    if text.startswith("/") or os.path.isabs(text) or _WINDOWS_ABS_PATTERN.match(text):
        return "absolute path"
    if "\x00" in text:
        return "NUL byte"
    return None


def _scan(node, where: str) -> None:
    if isinstance(node, str):
        hit = _forbidden_value(node)
        if hit:
            raise ValueError(f"manifest {where} must not contain a {hit}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _scan(item, f"{where}[{index}]")
    elif isinstance(node, dict):
        for key, item in node.items():
            _scan(item, f"{where}.{key}")


def canonical_digest(manifest: dict) -> str:
    """Canonical content digest (same bytes as replay evidence hashing)."""
    text = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _names(values, label: str, pattern, maximum: int) -> list:
    if type(values) is not list or len(values) > maximum:
        raise ValueError(f"manifest {label} must list at most {maximum} names")
    if (any(type(name) is not str or not pattern.match(name) for name in values)
            or len(set(values)) != len(values)):
        raise ValueError(f"invalid or duplicate manifest {label}")
    return list(values)


def validate_manifest(obj) -> dict:
    """Validate a parsed manifest object. Pure: no imports, no execution."""
    if type(obj) is not dict or set(obj) != _MANIFEST_KEYS:
        raise ValueError("manifest must carry exactly the v1 fields")
    if type(obj["manifest_version"]) is not int or obj["manifest_version"] != 1:
        raise ValueError("unsupported manifest_version")
    if (type(obj["world_version"]) is not str
            or not WORLD_PATTERN.match(obj["world_version"])):
        raise ValueError("invalid manifest world_version")
    if (type(obj["environment_id"]) is not str
            or not ENV_ID_PATTERN.match(obj["environment_id"])):
        raise ValueError("invalid manifest environment_id")
    if (type(obj["environment_version"]) is not str
            or not ENV_VERSION_PATTERN.match(obj["environment_version"])):
        raise ValueError("invalid manifest environment_version")
    if type(obj["adapter_ref"]) is not str or not obj["adapter_ref"]:
        raise ValueError("invalid manifest adapter_ref")
    if (type(obj["display_name"]) is not str or not obj["display_name"]
            or len(obj["display_name"]) > 200):
        raise ValueError("invalid manifest display_name")
    tools = obj["tools"]
    if type(tools) is not list or not 1 <= len(tools) <= 32:
        raise ValueError("manifest tools must list 1-32 tools")
    seen = set()
    for entry in tools:
        if type(entry) is not dict or set(entry) != _TOOL_KEYS:
            raise ValueError("each manifest tool needs exactly name and parameters")
        name, parameters = entry["name"], entry["parameters"]
        if (type(name) is not str or not TOOL_NAME_PATTERN.match(name)
                or name in seen):
            raise ValueError(f"invalid or duplicate manifest tool '{name}'")
        seen.add(name)
        if (type(parameters) is not list or len(parameters) > 16
                or any(type(p) is not str or not PARAM_PATTERN.match(p)
                       for p in parameters)
                or len(set(parameters)) != len(parameters)):
            raise ValueError(f"invalid manifest parameters for tool '{name}'")
    _names(obj["fixtures"], "fixtures", NAME_PATTERN, 32)
    _names(obj["fault_hooks"], "fault_hooks", NAME_PATTERN, 16)
    _names(obj["invariants"], "invariants", NAME_PATTERN, 32)
    capabilities = obj["capabilities"]
    if (type(capabilities) is not dict or len(capabilities) > 32
            or any(type(name) is not str or not NAME_PATTERN.match(name)
                   or type(have) is not bool for name, have in capabilities.items())):
        raise ValueError("manifest capabilities map names to booleans")
    _names(obj["reproduction_modes"], "reproduction_modes", MODE_PATTERN, 8)
    if type(obj["notes"]) is not str or len(obj["notes"]) > 2000:
        raise ValueError("invalid manifest notes")
    _scan(obj, "manifest")
    return json.loads(json.dumps(obj))


def parse_manifest_bytes(data: bytes) -> dict:
    """Parse size-capped manifest bytes, rejecting duplicate keys."""
    if type(data) is not bytes or len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds the size bound")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("manifest is not UTF-8") from error

    def reject_dupes(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"duplicate manifest key '{key}'")
            obj[key] = value
        return obj

    try:
        return json.loads(text, object_pairs_hook=reject_dupes)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid manifest JSON ({error})") from None
