"""Initial OpenAPI-to-tool importer: local JSON documents, resolved locally, never fetched.

:func:`import_from_path` reads one OpenAPI 3.x JSON document plus the local
``.json`` files it references, resolves ``$ref`` pointers without any network
access, and returns :class:`ToolSpec` definitions -- names, effects and flat
scalar parameter schemas. Behavior, fixtures, faults and invariants stay
developer-provided: the importer contributes tool definitions only, and the
qualification suite checks them against the adapter's own ``describe`` output.

Trust and bounds: only paths under an approved root directory are read
(``root_dir``, defaulting to the document's own directory). Any ``$ref`` with
a URL scheme, a network host, an absolute path, a non-``.json`` target or a
traversal outside the root is refused. Documents are size-bounded, the
reference graph is count/depth-bounded with cycle detection, and only flat
object request bodies (``string``/``integer``/``boolean`` properties) are
imported -- anything richer fails with an explicit error naming the
construct, so missing coverage is loud, never silent.

Standard library only: ``json``/``os`` paths. YAML documents are explicitly
unsupported (provide JSON).
"""

import json
import os
from collections.abc import Mapping
from copy import deepcopy

from ghostlab.extensions.environment import EFFECTS, ToolSpec

IMPORTER_VERSION = "ghostlab-openapi-import/1"

MAX_DOCUMENT_BYTES = 262144  # 256 KiB per local file (read cap, TOCTOU-safe)
MAX_LOCAL_FILES = 16  # referenced local files per import, besides the root document
MAX_REF_RESOLUTIONS = 256  # total $ref expansions per import
MAX_REF_DEPTH = 12  # nested $ref chain depth
MAX_TOOLS = 32  # matches the extension interface tool limit
MAX_PROPERTIES = 64  # flat parameters per operation

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
READ_METHODS = frozenset({"get", "head"})
PATH_ITEM_SHARED_KEYS = frozenset({"summary", "description", "servers"})
SUPPORTED_PARAM_TYPES = frozenset({"string", "integer", "boolean"})
BOUND_KEYS = ("minLength", "maxLength", "minimum", "maximum")
JSON_MEDIA = "application/json"


class OpenAPIImportError(ValueError):
    """A document or reference the offline importer refused."""


def _has_scheme(text: str) -> bool:
    """A URI scheme prefix (``http:``, ``file:``, ``C:``, ...) is present."""
    head, separator, _ = text.partition(":")
    if not separator or not head or not head[0].isascii() or not head[0].isalpha():
        return False
    return all(char.isascii() and (char.isalnum() or char in "+-.") for char in head)


def _check_extension(path: str, *, what: str) -> None:
    lowered = path.lower()
    if lowered.endswith((".yaml", ".yml")):
        raise OpenAPIImportError(f"{what} supports JSON only, not YAML: {path!r}")
    if not lowered.endswith(".json"):
        raise OpenAPIImportError(f"{what} reads only local .json files: {path!r}")


def _check_inside(root_abs: str, candidate: str, display: str, *, what: str) -> None:
    try:
        inside = os.path.commonpath([os.path.normcase(root_abs), os.path.normcase(candidate)])
    except ValueError:
        raise OpenAPIImportError(f"{what} escapes the approved root: {display!r}") from None
    if inside != os.path.normcase(root_abs):
        raise OpenAPIImportError(f"{what} escapes the approved root: {display!r}")


def _read_checked(candidate: str, display: str, *, what: str) -> dict:
    """Parse one approved file. The read itself is capped, TOCTOU-safe."""
    try:
        size = os.path.getsize(candidate)
    except OSError:
        raise OpenAPIImportError(f"{what} cannot read {display!r}") from None
    if size > MAX_DOCUMENT_BYTES:
        raise OpenAPIImportError(f"{what} exceeds {MAX_DOCUMENT_BYTES} bytes: {display!r}")
    try:
        with open(candidate, "rb") as handle:
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    except OSError:
        raise OpenAPIImportError(f"{what} cannot read {display!r}") from None
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise OpenAPIImportError(f"{what} exceeds {MAX_DOCUMENT_BYTES} bytes: {display!r}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise OpenAPIImportError(f"{what} is not UTF-8: {display!r}") from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        raise OpenAPIImportError(f"{what} is not valid JSON: {display!r}") from None
    if not isinstance(document, dict):
        raise OpenAPIImportError(f"{what} must be a JSON object: {display!r}")
    return document


def _rooted_path(root_abs: str, base_dir: str, ref_file: str, *, what: str) -> str:
    """A local reference target inside the approved root, or a refusal."""
    if os.path.isabs(ref_file):
        raise OpenAPIImportError(f"{what} refuses absolute paths: {ref_file!r}")
    if _has_scheme(ref_file) or ref_file.startswith(("//", "\\\\")):
        raise OpenAPIImportError(f"{what} refuses URL/network references: {ref_file!r}")
    candidate = os.path.normpath(os.path.join(base_dir, ref_file))
    _check_inside(root_abs, candidate, ref_file, what=what)
    return candidate


def _read_local_json(ref_file: str, *, root_abs: str, base_dir: str) -> tuple[str, dict]:
    if type(ref_file) is not str or not ref_file:
        raise OpenAPIImportError("Local $ref needs a non-empty path")
    _check_extension(ref_file, what="Local $ref")
    target = _rooted_path(root_abs, base_dir, ref_file, what="Local $ref")
    return target, _read_checked(target, ref_file, what="Local $ref")


def _read_entry(path: str, root_abs: str) -> tuple[str, dict]:
    if not os.path.isabs(path) and (_has_scheme(path) or path.startswith(("//", "\\\\"))):
        raise OpenAPIImportError(f"Document refuses URL/network paths: {path!r}")
    _check_extension(path, what="Document")
    if os.path.isabs(path):
        candidate = os.path.normpath(path)
    else:
        candidate = os.path.normpath(os.path.join(root_abs, path))
    _check_inside(root_abs, candidate, path, what="Document")
    return candidate, _read_checked(candidate, path, what="Document")


def load_openapi_document(path: str, *, root_dir: str | None = None) -> dict:
    """Read and parse one local OpenAPI JSON document (no ``$ref`` resolution)."""
    if type(path) is not str or not path:
        raise OpenAPIImportError("Document path must be a non-empty string")
    if root_dir is None:
        root_dir = os.path.dirname(os.path.abspath(path))
    if type(root_dir) is not str or not root_dir:
        raise OpenAPIImportError("root_dir must be a non-empty string")
    _, document = _read_entry(path, os.path.abspath(root_dir))
    return document


def resolve_local_refs(document: Mapping, *, root_dir: str, base_dir: str | None = None) -> dict:
    """Deep-copy ``document`` with every local ``$ref`` expanded.

    Internal pointers (``#/components/...``) resolve against the document that
    holds them; relative file references (``other.json#/pointer``) resolve
    against that document's directory and must stay under ``root_dir``.
    Cycles, excessive depth/counts and non-local targets raise
    :class:`OpenAPIImportError`. The input is never mutated.
    """
    if not isinstance(document, Mapping):
        raise OpenAPIImportError("Cannot resolve $refs in a non-object document")
    if type(root_dir) is not str or not root_dir:
        raise OpenAPIImportError("root_dir must be a non-empty string")
    root_abs = os.path.abspath(root_dir)
    if base_dir is None:
        base_abs = root_abs
    else:
        if type(base_dir) is not str or not base_dir:
            raise OpenAPIImportError("base_dir must be a non-empty string")
        base_abs = os.path.abspath(base_dir)
        _check_inside(root_abs, base_abs, base_dir, what="Base directory")
    files: dict[str, dict] = {}
    resolutions = [0]

    def load_relative(ref_file: str, base: str) -> tuple[str, dict]:
        target, parsed = _read_local_json(ref_file, root_abs=root_abs, base_dir=base)
        if target not in files:
            if len(files) >= MAX_LOCAL_FILES:
                raise OpenAPIImportError(f"Too many local files (>{MAX_LOCAL_FILES})")
            files[target] = parsed
        return target, files[target]

    def pointer(doc: object, fragment: str, *, where: str) -> object:
        if fragment == "":
            return doc
        if not fragment.startswith("/"):
            raise OpenAPIImportError(f"Bad $ref fragment in {where}: {fragment!r}")
        current = doc
        for raw_part in fragment[1:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, Mapping):
                if part not in current:
                    raise OpenAPIImportError(
                        f"Unknown $ref pointer in {where}: {fragment!r}")
                current = current[part]
            elif isinstance(current, list):
                if not part.isdigit() or int(part) >= len(current):
                    raise OpenAPIImportError(
                        f"Unknown $ref pointer in {where}: {fragment!r}")
                current = current[int(part)]
            else:
                raise OpenAPIImportError(f"Unknown $ref pointer in {where}: {fragment!r}")
        return current

    def expand(node: object, base: tuple, chain: tuple) -> object:
        if isinstance(node, Mapping):
            ref = node.get("$ref")
            if isinstance(ref, str):
                return expand_ref(ref, node, base, chain)
            return {key: expand(value, base, chain) for key, value in node.items()}
        if isinstance(node, list):
            return [expand(value, base, chain) for value in node]
        return node

    def expand_ref(ref: str, node: Mapping, base: tuple, chain: tuple) -> object:
        doc_id, doc, base_path = base
        resolutions[0] += 1
        if resolutions[0] > MAX_REF_RESOLUTIONS:
            raise OpenAPIImportError(f"Too many $ref expansions (>{MAX_REF_RESOLUTIONS})")
        if len(chain) >= MAX_REF_DEPTH:
            raise OpenAPIImportError(f"$ref chain deeper than {MAX_REF_DEPTH}")
        if not ref:
            raise OpenAPIImportError("Empty $ref")
        file_part, separator, fragment = ref.partition("#")
        if file_part:
            target, target_doc = load_relative(file_part, base_path)
            key = (target, "#" + fragment if separator else "#")
            next_base = (target, target_doc, os.path.dirname(target))
        else:
            if not separator:
                raise OpenAPIImportError(f"Bad $ref without a pointer: {ref!r}")
            key = (doc_id, "#" + fragment)
            next_base = base
        if key in chain:
            raise OpenAPIImportError(f"Circular $ref: {ref!r}")
        target_node = pointer(next_base[1], fragment, where=f"$ref {ref!r}")
        merged = deepcopy(target_node)
        siblings = {key: deepcopy(value) for key, value in node.items() if key != "$ref"}
        if siblings:
            if not isinstance(merged, Mapping):
                raise OpenAPIImportError(
                    f"Cannot merge siblings onto non-object $ref {ref!r}")
            merged = {**merged, **siblings}
        return expand(merged, next_base, chain + (key,))

    root_copy = deepcopy(dict(document))
    return expand(root_copy, ("<root>", root_copy, base_abs), ())


def _refute_refs(node: object, operation_id: str) -> None:
    if isinstance(node, Mapping):
        if "$ref" in node:
            raise OpenAPIImportError(
                f"Operation {operation_id!r} still holds an unresolved $ref"
                " (resolve refs first)")
        for value in node.values():
            _refute_refs(value, operation_id)
    elif isinstance(node, list):
        for value in node:
            _refute_refs(value, operation_id)


def _import_property(spec: object, operation_id: str, name: str) -> dict:
    if not isinstance(spec, Mapping):
        raise OpenAPIImportError(
            f"Operation {operation_id!r} property {name!r} must be an object")
    kind = spec.get("type")
    if kind not in SUPPORTED_PARAM_TYPES:
        raise OpenAPIImportError(
            f"Operation {operation_id!r} property {name!r} has unsupported type {kind!r}")
    converted = {"type": kind}
    for bound in BOUND_KEYS:
        if bound in spec:
            value = spec[bound]
            if type(value) is not int:
                raise OpenAPIImportError(
                    f"Operation {operation_id!r} property {name!r} bound {bound!r}"
                    " must be an integer")
            converted[bound] = value
    return converted


def _import_parameters(operation: Mapping, operation_id: str) -> dict:
    body = operation.get("requestBody")
    if body is None:
        return {"type": "object", "additionalProperties": False, "required": [],
                "properties": {}}
    if not isinstance(body, Mapping) or "$ref" in body:
        raise OpenAPIImportError(
            f"Operation {operation_id!r} has an unresolved requestBody ($ref)")
    content = body.get("content")
    if not isinstance(content, Mapping) or JSON_MEDIA not in content:
        raise OpenAPIImportError(
            f"Operation {operation_id!r} needs an {JSON_MEDIA} requestBody")
    media = content[JSON_MEDIA]
    if not isinstance(media, Mapping) or not isinstance(media.get("schema"), Mapping):
        raise OpenAPIImportError(f"Operation {operation_id!r} has no usable JSON schema")
    schema = media["schema"]
    _refute_refs(schema, operation_id)
    if schema.get("type") != "object":
        raise OpenAPIImportError(f"Operation {operation_id!r} needs a flat object schema")
    if schema.get("additionalProperties", False) is not False:
        raise OpenAPIImportError(
            f"Operation {operation_id!r} allows additional properties (unsupported)")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise OpenAPIImportError(f"Operation {operation_id!r} has a bad properties map")
    if len(properties) > MAX_PROPERTIES:
        raise OpenAPIImportError(
            f"Operation {operation_id!r} has too many properties (>{MAX_PROPERTIES})")
    required = schema.get("required", [])
    if type(required) is not list or any(type(name) is not str for name in required):
        raise OpenAPIImportError(f"Operation {operation_id!r} has a bad required list")
    unknown = sorted(set(required) - set(properties))
    if unknown:
        raise OpenAPIImportError(f"Operation {operation_id!r} requires unknown {unknown}")
    return {"type": "object", "additionalProperties": False, "required": sorted(required),
            "properties": {name: _import_property(properties[name], operation_id, name)
                           for name in sorted(properties)}}


def import_tool_definitions(document: Mapping, *, effect_map: Mapping | None = None,
                            ) -> list[ToolSpec]:
    """Extract tool definitions from a resolved OpenAPI 3.x document.

    Every operation needs a unique string ``operationId`` (the tool name).
    Effects default by method (``get``/``head`` read, the rest mutate) unless
    ``effect_map`` overrides them. Only ``application/json`` object request
    bodies become parameters; anything else raises :class:`OpenAPIImportError`
    naming the construct. Pass ``resolve_local_refs`` output: stray ``$ref``
    keys are refused rather than silently skipped.
    """
    if not isinstance(document, Mapping):
        raise OpenAPIImportError("OpenAPI document must be an object")
    version = document.get("openapi")
    if type(version) is not str or not version.startswith("3."):
        raise OpenAPIImportError(
            f"Only OpenAPI 3.x documents are supported (got {version!r})")
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        raise OpenAPIImportError("OpenAPI document has no paths to import")
    if effect_map is None:
        effect_map = {}
    if not isinstance(effect_map, Mapping):
        raise OpenAPIImportError("effect_map must map operation ids to effects")
    for key, value in effect_map.items():
        if type(key) is not str or value not in EFFECTS:
            raise OpenAPIImportError(f"Bad effect_map entry {key!r}: {value!r}")
    tools: list[ToolSpec] = []
    for path, item in paths.items():
        if type(path) is not str or not path.startswith("/"):
            raise OpenAPIImportError(f"Bad OpenAPI path {path!r}")
        if not isinstance(item, Mapping):
            raise OpenAPIImportError(f"Path {path!r} must be an object")
        if "parameters" in item:
            raise OpenAPIImportError(
                f"Path {path!r} uses path-level parameters (unsupported)")
        for method, operation in item.items():
            if method not in METHODS:
                if method in PATH_ITEM_SHARED_KEYS or (
                        type(method) is str and method.startswith("x-")):
                    continue
                if method == "$ref":
                    raise OpenAPIImportError(
                        f"Path {path!r} still holds an unresolved $ref (resolve refs first)")
                raise OpenAPIImportError(f"Unsupported path-item key {method!r} at {path!r}")
            if not isinstance(operation, Mapping):
                raise OpenAPIImportError(f"Operation {path} {method} must be an object")
            operation_id = operation.get("operationId")
            if (type(operation_id) is not str or not operation_id
                    or len(operation_id) > 64):
                raise OpenAPIImportError(
                    f"Operation {path} {method} needs a short string operationId")
            if any(tool.name == operation_id for tool in tools):
                raise OpenAPIImportError(f"Duplicate operationId {operation_id!r}")
            if "parameters" in operation:
                raise OpenAPIImportError(
                    f"Operation {operation_id!r} uses parameters"
                    " (only JSON requestBody is supported)")
            effect = effect_map.get(operation_id,
                                    "read" if method in READ_METHODS else "mutate")
            parameters = _import_parameters(operation, operation_id)
            description = operation.get("summary") or operation.get("description") or ""
            if type(description) is not str:
                raise OpenAPIImportError(
                    f"Operation {operation_id!r} has a non-string summary")
            tools.append(ToolSpec(name=operation_id, effect=effect, parameters=parameters,
                                  description=description[:2000]))
    if len(tools) > MAX_TOOLS:
        raise OpenAPIImportError(f"Too many operations (>{MAX_TOOLS})")
    unused = sorted(set(effect_map) - {tool.name for tool in tools})
    if unused:
        raise OpenAPIImportError(f"effect_map names unknown operations: {unused}")
    if not tools:
        raise OpenAPIImportError("OpenAPI document defines no operations")
    return tools


def import_from_path(path: str, *, root_dir: str | None = None,
                     effect_map: Mapping | None = None) -> list[ToolSpec]:
    """Load, resolve and import one local OpenAPI JSON document.

    ``root_dir`` approves every file the import may read; it defaults to the
    document's own directory. Relative references resolve against the holding
    document's directory. Nothing is fetched: non-local targets are refused.
    """
    if type(path) is not str or not path:
        raise OpenAPIImportError("Document path must be a non-empty string")
    if root_dir is None:
        root_dir = os.path.dirname(os.path.abspath(path))
    if type(root_dir) is not str or not root_dir:
        raise OpenAPIImportError("root_dir must be a non-empty string")
    root_abs = os.path.abspath(root_dir)
    target, document = _read_entry(path, root_abs)
    resolved = resolve_local_refs(document, root_dir=root_abs,
                                  base_dir=os.path.dirname(target))
    return import_tool_definitions(resolved, effect_map=effect_map)
