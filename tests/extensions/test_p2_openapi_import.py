"""P2 OpenAPI import: local JSON tool definitions, never fetched.

Standard library only (unittest style). Covers the sample reservation
document end to end (imported definitions equal the adapter's own schemas),
approved local $refs, and every refusal: URL/network targets, absolute paths,
path traversal, bounds, cycles, and unsupported constructs.
"""

import json
import tempfile
import unittest
from pathlib import Path

import ghostlab.extensions.openapi_import as importer_module
from ghostlab.adapters.reservation_env import RESERVATION_TOOL_SCHEMAS, TOOL_EFFECTS
from ghostlab.extensions.openapi_import import (
    MAX_DOCUMENT_BYTES, OpenAPIImportError, import_from_path, import_tool_definitions,
    load_openapi_document, resolve_local_refs)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "reservation_openapi.json"


def doc(paths, components=None):
    document = {"openapi": "3.1.0", "info": {"title": "t", "version": "1"},
                "paths": paths}
    if components is not None:
        document["components"] = components
    return document


def get_op(operation_id):
    return {"operationId": operation_id, "responses": {"200": {"description": "ok"}}}


def post_op(operation_id, schema):
    return {"operationId": operation_id,
            "requestBody": {"content": {"application/json": {"schema": schema}}},
            "responses": {"200": {"description": "ok"}}}


def obj(required, properties, **extra):
    schema = {"type": "object", "required": list(required), "properties": dict(properties)}
    schema.update(extra)
    return schema


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def put(self, name, payload):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, bytes):
            target.write_bytes(payload)
        elif isinstance(payload, str):
            target.write_text(payload, encoding="utf-8")
        else:
            target.write_text(json.dumps(payload), encoding="utf-8")
        return str(target)


class SampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # get_status travels over POST (JSON body) but only reads: the override is the
        # documented mechanism for effects the method heuristic cannot see.
        cls.tools = import_from_path(str(EXAMPLE), effect_map={"get_status": "read"})

    def test_sample_imports_four_tools(self):
        self.assertEqual([tool.name for tool in self.tools],
                         ["check_availability", "reserve", "cancel", "get_status"])
        self.assertEqual([tool.effect for tool in self.tools],
                         ["read", "mutate", "mutate", "read"])

    def test_imported_definitions_equal_adapter_schemas(self):
        for tool in self.tools:
            self.assertEqual(tool.parameters, RESERVATION_TOOL_SCHEMAS[tool.name],
                             tool.name)
            self.assertEqual(tool.effect, TOOL_EFFECTS[tool.name], tool.name)

    def test_effect_map_override(self):
        tools = import_from_path(str(EXAMPLE), effect_map={"get_status": "deliver"})
        by_name = {tool.name: tool for tool in tools}
        self.assertEqual(by_name["get_status"].effect, "deliver")
        with self.assertRaises(OpenAPIImportError):
            import_from_path(str(EXAMPLE), effect_map={"nope": "read"})
        with self.assertRaises(OpenAPIImportError):
            import_from_path(str(EXAMPLE), effect_map={"reserve": "explode"})


class LocalRefTests(TempCase):
    def test_internal_refs_resolve(self):
        document = doc({"/items": {"post": post_op(
            "create", {"$ref": "#/components/schemas/Form"})}},
            {"schemas": {"Form": obj(
                ["name"], {"name": {"$ref": "#/components/schemas/Name"}}),
                "Name": {"type": "string", "minLength": 1, "maxLength": 9}}})
        before = json.loads(json.dumps(document))
        tools = import_tool_definitions(
            resolve_local_refs(document, root_dir=str(self.root)))
        self.assertEqual(document, before)  # The input is never mutated.
        self.assertEqual(tools[0].parameters["properties"]["name"],
                         {"type": "string", "minLength": 1, "maxLength": 9})

    def test_sibling_keys_merge_onto_refs(self):
        document = doc({"/items": {"post": post_op(
            "create", obj(["name"], {"name": {"$ref": "#/components/schemas/Base",
                                             "maxLength": 5}}))}},
            {"schemas": {"Base": {"type": "string"}}})
        tools = import_tool_definitions(
            resolve_local_refs(document, root_dir=str(self.root)))
        self.assertEqual(tools[0].parameters["properties"]["name"],
                         {"type": "string", "maxLength": 5})

    def test_local_file_refs_resolve(self):
        self.put("shared.json", {"components": {"schemas": {
            "Name": {"type": "string", "minLength": 1}}}})
        main = self.put("main.json", doc({"/items": {"post": post_op(
            "create", {"$ref": "#/components/schemas/Wrapper"})}},
            {"schemas": {"Wrapper": obj(
                ["name"], {"name": {"$ref": "shared.json#/components/schemas/Name"}})}}))
        tools = import_from_path(main)
        self.assertEqual(tools[0].parameters["properties"]["name"],
                         {"type": "string", "minLength": 1})

    def test_nested_relative_refs_follow_the_holding_document(self):
        self.put("sub/leaf.json", {"type": "string", "maxLength": 3})
        self.put("sub/mid.json", obj(["tag"], {"tag": {"$ref": "leaf.json"}}))
        main = self.put("main.json", doc({"/items": {"post": post_op(
            "create", {"$ref": "sub/mid.json"})}}))
        tools = import_from_path(main)
        self.assertEqual(tools[0].parameters["properties"]["tag"],
                         {"type": "string", "maxLength": 3})


class RefusalTests(TempCase):
    def refuses(self, ref, root=None):
        main = self.put("main.json", doc({"/items": {"post": post_op("create", ref)}}))
        with self.assertRaises(OpenAPIImportError, msg=ref):
            import_from_path(main, root_dir=root or str(self.root))

    def test_url_and_network_refs_are_refused(self):
        for ref in ({"$ref": "http://127.0.0.1:9/x.json#/s"},
                    {"$ref": "https://example.com/s.json"},
                    {"$ref": "file:///etc/x.json"},
                    {"$ref": "//host/x.json"}):
            self.refuses(ref)
        main = self.put("main.json", doc({"/p": {"get": get_op("ok")}}))
        with self.assertRaises(OpenAPIImportError) as held:
            import_from_path(main, root_dir=str(self.root))
            resolve_local_refs({"$ref": "http://127.0.0.1:9/x.json"},
                               root_dir=str(self.root))
        self.assertIn("URL", str(held.exception))

    def test_absolute_paths_are_refused(self):
        target = self.put("real.json", {"type": "string"})
        self.refuses({"$ref": target + "#/x"})
        self.refuses({"$ref": target})

    def test_traversal_outside_the_root_is_refused(self):
        self.put("sibling.json", {"type": "string"})
        (self.root / "inner").mkdir()
        main = self.put("inner/main.json", doc({"/items": {"post": post_op(
            "create", obj(["name"], {"name": {"$ref": "../sibling.json"}}))}}))
        with self.assertRaises(OpenAPIImportError) as held:
            import_from_path(main, root_dir=str(self.root / "inner"))
        self.assertIn("root", str(held.exception))

    def test_missing_and_foreign_files_are_refused(self):
        self.refuses({"$ref": "absent.json"})
        self.put("notes.yaml", "openapi: 3.1.0")
        self.refuses({"$ref": "notes.yaml"})
        self.put("notes.txt", "{}")
        self.refuses({"$ref": "notes.txt"})

    def test_yaml_refs_name_json_only(self):
        self.put("notes.yaml", "openapi: 3.1.0")
        main = self.put("main.json", doc({"/items": {"post": post_op(
            "create", {"$ref": "notes.yaml"})}}))
        with self.assertRaises(OpenAPIImportError) as held:
            import_from_path(main)
        self.assertIn("JSON", str(held.exception))

    def test_bad_pointers_are_refused(self):
        document = doc({"/p": {"get": get_op("ok")}},
                       {"schemas": {"S": {"type": "string"}}})
        for ref in ("#/components/schemas/Missing", "#nope", "", "#/components/schemas/S/x"):
            with self.assertRaises(OpenAPIImportError, msg=ref):
                resolve_local_refs({"$ref": ref, **document}, root_dir=str(self.root))

    def test_cycles_are_refused(self):
        self.put("a.json", {"$ref": "b.json"})
        self.put("b.json", {"$ref": "a.json"})
        self.refuses({"$ref": "a.json"})
        with self.assertRaises(OpenAPIImportError):
            resolve_local_refs({"$ref": "#"}, root_dir=str(self.root))

    def test_deep_chains_are_refused(self):
        for index in range(12):
            self.put(f"f{index}.json", {"$ref": f"f{index + 1}.json"})
        self.put("f12.json", obj([], {}))
        self.refuses({"$ref": "f0.json"})

    def test_ref_floods_are_refused(self):
        many = {f"p{index}": {"$ref": "#/components/schemas/S"} for index in range(300)}
        document = doc({"/p": {"get": get_op("ok")}},
                       {"schemas": {"S": {"type": "string"}, "Big": obj([], many)}})
        with self.assertRaises(OpenAPIImportError):
            resolve_local_refs(document, root_dir=str(self.root))
        few = {f"p{index}": {"$ref": "#/components/schemas/S"} for index in range(200)}
        small = doc({"/p": {"get": get_op("ok")}},
                    {"schemas": {"S": {"type": "string"}, "Big": obj([], few)}})
        resolved = resolve_local_refs(small, root_dir=str(self.root))
        self.assertEqual(resolved["components"]["schemas"]["Big"]["properties"]["p0"],
                         {"type": "string"})


class EntryTests(TempCase):
    def test_entry_paths_are_validated(self):
        with self.assertRaises(OpenAPIImportError):
            load_openapi_document("https://example.com/x.json")
        with self.assertRaises(OpenAPIImportError):
            load_openapi_document("", root_dir=str(self.root))
        with self.assertRaises(OpenAPIImportError):
            load_openapi_document(None)
        main = self.put("a/main.json", doc({"/p": {"get": get_op("ok")}}))
        with self.assertRaises(OpenAPIImportError):
            load_openapi_document(main, root_dir=str(self.root / "elsewhere"))
        self.put("page.yaml", "openapi: 3.1.0")
        with self.assertRaises(OpenAPIImportError) as held:
            load_openapi_document(str(self.root / "page.yaml"))
        self.assertIn("JSON", str(held.exception))

    def test_entry_content_is_validated(self):
        self.put("missing.json", "{}")
        with self.assertRaises(OpenAPIImportError):
            load_openapi_document(str(self.root / "absent.json"))
        self.put("text.json", "not json")
        with self.assertRaises(OpenAPIImportError):
            load_openapi_document(str(self.root / "text.json"))
        self.put("list.json", "[1, 2]")
        with self.assertRaises(OpenAPIImportError):
            load_openapi_document(str(self.root / "list.json"))

    def test_size_bound_has_an_exact_edge(self):
        base = doc({"/p": {"get": get_op("ok")}})
        overhead = len(json.dumps({**base, "_pad": ""}).encode("utf-8"))
        fitting = dict(base)
        fitting["_pad"] = "x" * (MAX_DOCUMENT_BYTES - overhead)
        raw = json.dumps(fitting).encode("utf-8")
        self.assertEqual(len(raw), MAX_DOCUMENT_BYTES)
        edge = self.put("edge.json", raw)
        self.assertEqual(load_openapi_document(edge)["openapi"], "3.1.0")
        with self.assertRaises(OpenAPIImportError):
            load_openapi_document(self.put("over.json", raw + b" "))


class ConstructTests(unittest.TestCase):
    def imports(self, paths, **kwargs):
        return import_tool_definitions(doc(paths, kwargs.get("components")))

    def test_document_shape_is_validated(self):
        with self.assertRaises(OpenAPIImportError):
            import_tool_definitions({"paths": {"/p": {"get": get_op("ok")}}})
        with self.assertRaises(OpenAPIImportError):
            import_tool_definitions(doc({}, components=None) | {"openapi": "2.0"})
        self.assertEqual(len(import_tool_definitions(
            doc({"/p": {"get": get_op("ok")}}) | {"openapi": "3.0.0"})), 1)
        with self.assertRaises(OpenAPIImportError):
            import_tool_definitions(doc({}))
        with self.assertRaises(OpenAPIImportError):
            self.imports({"nope": {"get": get_op("ok")}})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": "nope"})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"parameters": [], "get": get_op("ok")}})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"bogus": {}}})
        shared = self.imports({"/p": {"summary": "s", "x-extra": 1, "get": get_op("ok")}})
        self.assertEqual(len(shared), 1)
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"x-extra": 1}})

    def test_operations_need_unique_ids(self):
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"get": "nope"}})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"get": {"responses": {}}}})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"get": {"operationId": "x" * 65}}})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/a": {"get": get_op("same")}, "/b": {"get": get_op("same")}})

    def test_effects_default_by_method(self):
        paths = {f"/{method}": {method: get_op(method)} for method in
                 ("get", "put", "post", "delete", "patch", "head", "options", "trace")}
        by_name = {tool.name: tool.effect for tool in self.imports(paths)}
        self.assertEqual(by_name,
                         {"get": "read", "head": "read", "put": "mutate", "post": "mutate",
                          "delete": "mutate", "patch": "mutate", "options": "mutate",
                          "trace": "mutate"})

    def test_request_bodies_are_validated(self):
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": {**post_op("create", obj([], {})),
                                                 "parameters": []}}})
        bodyless = {"operationId": "create", "requestBody": {"content": {
            "text/plain": {"schema": {"type": "string"}}}}}
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": bodyless}})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": post_op("create", {"type": "array"})}})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": post_op(
                "create", obj([], {}, additionalProperties=True))}})
        not_a_map = obj([], {})
        not_a_map["properties"] = []
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": post_op("create", not_a_map)}})
        crowded = {f"p{index}": {"type": "string"} for index in range(65)}
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": post_op("create", obj([], crowded))}})
        bad_required = obj([], {"a": {"type": "string"}})
        bad_required["required"] = "a"
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": post_op("create", bad_required)}})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": post_op(
                "create", obj(["ghost"], {"a": {"type": "string"}}))}})

    def test_properties_are_validated(self):
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": post_op(
                "create", obj(["a"], {"a": "nope"}))}})
        for bad in ({"type": "number"}, {"type": "array"}, {}):
            with self.assertRaises(OpenAPIImportError, msg=bad):
                self.imports({"/p": {"post": post_op(
                    "create", obj(["a"], {"a": bad}))}})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": post_op(
                "create", obj(["a"], {"a": {"type": "string", "maxLength": "many"}}))}})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": post_op(
                "create", obj(["a"], {"a": {"type": "integer", "minimum": True}}))}})

    def test_descriptions_fall_back_and_truncate(self):
        long = self.imports({"/p": {"post": {**post_op("create", obj([], {})),
                                                 "summary": "s" * 3000}}})
        self.assertEqual(len(long[0].description), 2000)
        fallback = self.imports({"/p": {"post": {**post_op("create", obj([], {})),
                                                     "description": "d"}}})
        self.assertEqual(fallback[0].description, "d")
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": {**post_op("create", obj([], {})),
                                             "summary": 42}}})

    def test_tool_count_is_bounded(self):
        paths = {f"/p{index}": {"get": get_op(f"op{index}")} for index in range(33)}
        with self.assertRaises(OpenAPIImportError):
            self.imports(paths)

    def test_unresolved_refs_are_refused_not_skipped(self):
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"post": post_op("create", {"$ref": "#/components/x"})}},
                         components={"x": obj([], {})})
        with self.assertRaises(OpenAPIImportError):
            self.imports({"/p": {"$ref": "#/components/x"}},
                         components={"x": {"get": get_op("ok")}})
        with self.assertRaises(OpenAPIImportError):
            import_tool_definitions("nope")
        with self.assertRaises(OpenAPIImportError):
            import_tool_definitions(doc({"/p": {"get": get_op("ok")}}), effect_map=[])
        with self.assertRaises(OpenAPIImportError):
            import_tool_definitions(doc({"/p": {"get": get_op("ok")}}),
                                    effect_map={42: "read"})


class NoFetchTests(unittest.TestCase):
    def test_refusal_names_urls_without_connecting(self):
        with self.assertRaises(OpenAPIImportError) as held:
            resolve_local_refs({"$ref": "http://127.0.0.1:9/x.json#/s"},
                               root_dir=".")
        self.assertIn("URL", str(held.exception))

    def test_importer_is_stdlib_only(self):
        source = Path(importer_module.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                self.assertNotRegex(
                    line, r"\b(socket|urllib|requests|httpx|http\.client)\b", line)


if __name__ == "__main__":
    unittest.main()
