"""Bounded JSON manifest import referencing an existing registration.

A manifest declares what an environment claims to offer; it carries no code,
no package installs, no URLs and no filesystem paths. Unknown fields are
rejected, so ``pip``/``code``/``url``/``path`` style keys can never sneak in.
Validation is pure: it never imports adapter code and never runs anything.
"""

import hashlib
import json
import os
import re

from .registry import (ENV_ID_PATTERN, ENV_VERSION_PATTERN, MODE_PATTERN,
                       PARAM_PATTERN, TOOL_NAME_PATTERN)

MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 262_144

_MANIFEST_KEYS = {"manifest_version", "env_id", "env_version", "adapter_ref",
                  "display_name", "tools", "reproduction_modes", "fault_hooks",
                  "notes"}
_TOOL_KEYS = {"name", "parameters"}
# Hook names use underscores (e.g. timeout_after_commit), unlike modes.
_HOOK_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")
_WINDOWS_ABS_PATTERN = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")


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
    text = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def validate_manifest(obj) -> dict:
    """Validate a parsed manifest object. Pure: no imports, no execution."""
    if type(obj) is not dict or set(obj) != _MANIFEST_KEYS:
        raise ValueError("manifest must carry exactly the v1 fields")
    if type(obj["manifest_version"]) is not int or obj["manifest_version"] != 1:
        raise ValueError("unsupported manifest_version")
    if type(obj["env_id"]) is not str or not ENV_ID_PATTERN.match(obj["env_id"]):
        raise ValueError("invalid manifest env_id")
    if (type(obj["env_version"]) is not str
            or not ENV_VERSION_PATTERN.match(obj["env_version"])):
        raise ValueError("invalid manifest env_version")
    if type(obj["adapter_ref"]) is not str or not obj["adapter_ref"]:
        raise ValueError("invalid manifest adapter_ref")
    if (type(obj["display_name"]) is not str or not obj["display_name"]
            or len(obj["display_name"]) > 200):
        raise ValueError("invalid manifest display_name")
    tools = obj["tools"]
    if type(tools) is not list or not 1 <= len(tools) <= 32:
        raise ValueError("manifest tools must list 1-32 tools")
    seen_tools = set()
    for entry in tools:
        if type(entry) is not dict or set(entry) != _TOOL_KEYS:
            raise ValueError("manifest tool entries need exactly name and parameters")
        name, parameters = entry["name"], entry["parameters"]
        if (type(name) is not str or not TOOL_NAME_PATTERN.match(name)
                or name in seen_tools):
            raise ValueError(f"invalid or duplicate manifest tool '{name}'")
        seen_tools.add(name)
        if (type(parameters) is not list or len(parameters) > 16
                or any(type(p) is not str or not PARAM_PATTERN.match(p)
                       for p in parameters)
                or len(set(parameters)) != len(parameters)):
            raise ValueError(f"invalid parameters for manifest tool '{name}'")
    for key, pattern, low, high in (("reproduction_modes", MODE_PATTERN, 1, 8),
                                    ("fault_hooks", _HOOK_PATTERN, 0, 16)):
        values = obj[key]
        if type(values) is not list or not low <= len(values) <= high:
            raise ValueError(f"manifest {key} has invalid length")
        if (any(type(v) is not str or not pattern.match(v) for v in values)
                or len(set(values)) != len(values)):
            raise ValueError(f"invalid or duplicate manifest {key} entry")
    if type(obj["notes"]) is not str or len(obj["notes"]) > 2000:
        raise ValueError("invalid manifest notes")
    _scan(obj, "manifest")
    return json.loads(json.dumps(obj))


def validate_manifest_bytes(data: bytes) -> dict:
    """Validate raw manifest bytes under the size bound. Pure."""
    if type(data) is not bytes:
        raise ValueError("manifest input must be bytes")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds the size bound")
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("manifest is not valid JSON") from error
    return validate_manifest(obj)


def import_manifest(registry, data: bytes) -> dict:
    """Validate a manifest and store it against its existing registration.

    The manifest's ``env_id`` must already be registered, and its
    ``env_version``/``adapter_ref`` must match that registration exactly.
    Stores data only; never imports or executes the environment.
    """
    manifest = validate_manifest_bytes(data)
    registration = registry.get(manifest["env_id"])
    if manifest["env_version"] != registration["env_version"]:
        raise ValueError("manifest env_version does not match the registration")
    if manifest["adapter_ref"] != registration["adapter_ref"]:
        raise ValueError("manifest adapter_ref does not match the registration")
    digest = canonical_digest(manifest)
    stored = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=True, allow_nan=False)
    registry.store_manifest(manifest["env_id"], digest, stored)
    return {"manifest_digest": digest, "env_id": manifest["env_id"],
            "env_version": manifest["env_version"]}
