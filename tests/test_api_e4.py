"""Track E4: live service layer + UI live-client binding.

Drives EVERY route over real HTTP (127.0.0.1, ephemeral port, urllib)
against track-private temp DBs. Scans every response body for
secret patterns. No model calls, no secrets.
"""

import copy
import gc
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ghostlab.api.app import create_app, default_paths
from ghostlab.envs.adapters import REFUND_MODES, REFUND_TOOLS
from ghostlab.envs.registry import EnvRegistry
from ghostlab.experiments import ops
from ghostlab.experiments.configs import canonical_digest
from ghostlab.experiments.store import ExperimentStore

REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"
CANARY = "E4-CANARY-9d2c7b1a-do-not-echo"

SECRET_PATTERNS = [
    re.compile(r"model_api_key\s*[:=]\s*['\"]?[^'\"\s]{4,}", re.I),
    re.compile(r"baseten_api_key\s*[:=]\s*['\"]?[^'\"\s]{4,}", re.I),
    re.compile(r"api[_-]?key\s*[:=]\s*['\"][^'\"]{8,}['\"]", re.I),
    re.compile(r"begin [a-z ]*private key", re.I),
    re.compile(r"bearer\s+[A-Za-z0-9\-._~+/]{12,}={0,2}", re.I),
    re.compile(r"client_secret", re.I),
    re.compile(r"xox-", re.I),
]


def default_effective(**overrides):
    config = {
        "schemaVersion": 1,
        "env": {"envId": "refund-env", "envVersion": "refund-env/1",
                "adapterRef": REFUND_REF, "manifestDigest": None},
        "agent": {"adapter": "vulnerable", "note": ""},
        "fixture": {"orderId": "synthetic-order-1",
                    "customerId": "synthetic-customer-1",
                    "paidMinor": 250000, "currency": "INR"},
        "faults": {"timeoutAfterCommit": True},
        "checks": {"invariants": ["authorized_refunds",
                                  "total_refund_within_payment",
                                  "no_duplicate_full_refund",
                                  "unique_idempotency_keys"]},
        "limits": {"maxToolCalls": 20, "maxModelCalls": 20,
                   "maxInputTokens": 200000, "maxOutputTokens": 20000,
                   "maxWallClockMs": 600000, "maxParallelRuns": 2,
                   "maxCostMinor": None, "costCurrency": None},
        "objective": "Find duplicate-refund failures after a payment timeout",
        "successCriteria": ["A failing invariant is reproduced"],
    }
    for key, value in overrides.items():
        config[key] = value
    return config


def valid_manifest(env_id="refund-env", **overrides):
    manifest = {
        "manifest_version": 1, "env_id": env_id,
        "env_version": "refund-env/1", "adapter_ref": REFUND_REF,
        "display_name": "Synthetic refund",
        "tools": copy.deepcopy(REFUND_TOOLS),
        "reproduction_modes": list(REFUND_MODES),
        "fault_hooks": ["timeout_after_commit"],
        "notes": "reviewed local adapter",
    }
    manifest.update(overrides)
    return manifest


def valid_policy(**overrides):
    policy = {"policy_version": "policy/1", "ceiling_tool_calls": 20,
              "ceiling_total_tool_calls": 200, "ceiling_repetitions": 5,
              "ceiling_wall_clock_ms": 60000,
              "allowed_agents": ["vulnerable", "repaired"],
              "allowed_hooks": ["timeout_after_commit"]}
    policy.update(overrides)
    return policy


def valid_requested(**overrides):
    obj = {"config_version": 1, "name": "refund timeout probe",
           "env_id": "refund-env", "agent": "vulnerable",
           "fixture": {"order_id": "synthetic-order-1",
                       "customer_id": "synthetic-customer-1",
                       "paid_minor": 250000, "currency": "INR"},
           "faults": [],
           "rules": ["authorized_refunds", "total_refund_within_payment",
                     "no_duplicate_full_refund", "unique_idempotency_keys"],
           "limits": {"max_tool_calls": 20, "max_total_tool_calls": 200,
                      "max_wall_clock_ms": 60000},
           "repetitions": {"count": 1}}
    obj.update(overrides)
    return obj


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def start_server(app):
    import uvicorn

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            request = urllib.request.Request(
                base + "/v1/envs",
                headers={"X-GhostLab-Project": "probe"},
                method="GET")
            with urllib.request.urlopen(request, timeout=2):
                break
        except Exception:
            time.sleep(0.1)
    else:
        server.should_exit = True
        thread.join(timeout=10)
        raise RuntimeError("E4 test server did not start")

    def shutdown():
        server.should_exit = True
        thread.join(timeout=15)

    return base, shutdown


class E4Case(unittest.TestCase):
    def setUp(self):
        tmp_base = tempfile.gettempdir()
        self.tmp = tempfile.TemporaryDirectory(prefix="e4-", dir=tmp_base)
        self.addCleanup(self.tmp.cleanup)
        self.registry_path = str(
            Path(self.tmp.name) / "env.sqlite")
        self.store_path = str(
            Path(self.tmp.name) / "experiments.sqlite")
        self._shutdown = None
        self.addCleanup(self._stop_server)
        self.addCleanup(gc.collect)

    def _stop_server(self):
        if self._shutdown is not None:
            try:
                self._shutdown()
            except Exception:
                pass
            self._shutdown = None

    def _register(self, env_id="refund-env", qualify=False):
        registry = EnvRegistry(self.registry_path)
        registry.register(
            env_id=env_id, env_version="refund-env/1",
            adapter_ref=REFUND_REF, display_name="Synthetic refund",
            declared_tools=copy.deepcopy(REFUND_TOOLS),
            declared_modes=list(REFUND_MODES))
        if qualify:
            from ghostlab.envs.qualification import run_qualification

            run_qualification(registry, env_id)
        return registry

    def _serve(self):
        app = create_app(registry_path=self.registry_path,
                         store_path=self.store_path)
        base, shutdown = start_server(app)
        self._shutdown = shutdown
        return base

    def _call(self, base, method, path, body=None, project="proj-e4",
              raw=None, content_type="application/json"):
        headers = {}
        if project is not None:
            headers["X-GhostLab-Project"] = project
        data = None
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
                payload = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            payload = error.read()
        text = payload.decode("utf-8", errors="replace")
        for pattern in SECRET_PATTERNS:
            self.assertIsNone(
                pattern.search(text),
                f"secret pattern {pattern.pattern} in {method} {path}")
        self.assertNotIn(CANARY, text,
                         f"canary leaked in {method} {path}")
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
        return status, parsed, text

    def _poll_group(self, base, group_id, project="proj-e4", timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, parsed, _ = self._call(
                base, "GET", f"/v1/runs/{group_id}", project=project)
            self.assertEqual(status, 200, parsed)
            if parsed["status"] != "running":
                return parsed
            time.sleep(0.1)
        self.fail(f"group {group_id} still running after {timeout}s")

    def test_env_list_and_headers(self):
        self._register(qualify=False)
        base = self._serve()
        status, parsed, _ = self._call(base, "GET", "/v1/envs")
        self.assertEqual(status, 200)
        self.assertEqual(parsed["project_id"], "proj-e4")
        ids = [item["env_id"] for item in parsed["envs"]]
        self.assertIn("refund-env", ids)
        row = next(item for item in parsed["envs"]
                   if item["env_id"] == "refund-env")
        self.assertEqual(row["status"], "not-qualified")

        status, parsed, _ = self._call(base, "GET", "/v1/envs",
                                       project=None)
        self.assertEqual(status, 400)
        self.assertEqual(set(parsed), {"error", "detail"})

        status, parsed, _ = self._call(base, "GET", "/v1/envs",
                                       project="bad id!")
        self.assertEqual(status, 400)
        self.assertEqual(set(parsed), {"error", "detail"})

        status, parsed, _ = self._call(base, "GET", "/v1/envs",
                                       project="proj-other")
        self.assertEqual(status, 200)

    def test_import_manifest_pure_and_capabilities(self):
        self._register(qualify=False)
        base = self._serve()
        manifest = valid_manifest()
        status, parsed, _ = self._call(
            base, "POST", "/v1/envs/import-manifest",
            body={"manifest": manifest})
        self.assertEqual(status, 200, parsed)
        self.assertRegex(parsed["manifest_digest"], r"^[0-9a-f]{64}$")
        digest = parsed["manifest_digest"]

        status, listed, _ = self._call(base, "GET", "/v1/envs")
        row = next(item for item in listed["envs"]
                   if item["env_id"] == "refund-env")
        self.assertEqual(row["status"], "not-qualified")

        status, found, _ = self._call(
            base, "GET", "/v1/envs/refund-env/capabilities")
        self.assertEqual(status, 200, found)
        self.assertTrue(found["match"])
        self.assertEqual(len(found["discovered_tools"]), 3)

        status, found, _ = self._call(
            base, "GET",
            f"/v1/envs/refund-env/capabilities?manifest_digest={digest}")
        self.assertEqual(status, 200)
        self.assertEqual(found["manifest_digest"], digest)
        self.assertTrue(found["match"])

        bad = valid_manifest()
        del bad["notes"]
        status, parsed, _ = self._call(
            base, "POST", "/v1/envs/import-manifest",
            body={"manifest": bad})
        self.assertEqual(status, 400)
        self.assertEqual(set(parsed), {"error", "detail"})

        ghost = valid_manifest(env_id="ghost-env")
        status, parsed, _ = self._call(
            base, "POST", "/v1/envs/import-manifest",
            body={"manifest": ghost})
        self.assertEqual(status, 404)

        status, parsed, _ = self._call(
            base, "GET", "/v1/envs/ghost-env/capabilities")
        self.assertEqual(status, 404)

        status, parsed, _ = self._call(
            base, "GET",
            "/v1/envs/refund-env/capabilities?manifest_digest=" + "0" * 64)
        self.assertEqual(status, 404)

        self._register(env_id="second-env", qualify=False)
        registry = EnvRegistry(self.registry_path)
        from ghostlab.envs.manifest import import_manifest

        second = import_manifest(
            registry, json.dumps(
                valid_manifest(env_id="second-env")).encode())
        status, parsed, _ = self._call(
            base, "GET",
            "/v1/envs/refund-env/capabilities?manifest_digest="
            + second["manifest_digest"])
        self.assertEqual(status, 400)

    def test_qualify_and_report(self):
        registry = self._register(qualify=False)
        from ghostlab.envs.manifest import import_manifest

        imported = import_manifest(
            registry, json.dumps(valid_manifest()).encode())
        base = self._serve()

        status, parsed, _ = self._call(
            base, "GET", "/v1/envs/refund-env/report")
        self.assertEqual(status, 404)

        status, parsed, _ = self._call(
            base, "POST", "/v1/envs/refund-env/qualify",
            body={"manifest_digest": imported["manifest_digest"]})
        self.assertEqual(status, 200, parsed)
        self.assertEqual(parsed["report"]["status"],
                         "qualified-for-declared")
        self.assertEqual(len(parsed["report"]["checks"]), 7)

        status, latest, _ = self._call(
            base, "GET", "/v1/envs/refund-env/report")
        self.assertEqual(status, 200)
        self.assertEqual(latest["report"]["report_id"],
                         parsed["report"]["report_id"])

        status, parsed, _ = self._call(
            base, "POST", "/v1/envs/refund-env/qualify",
            body={"manifest_digest": "0" * 64})
        self.assertEqual(status, 404)

        status, parsed, _ = self._call(
            base, "POST", "/v1/envs/refund-env/qualify",
            body={"manifest_digest": None, "bogus": 1})
        self.assertEqual(status, 400)

        status, parsed, _ = self._call(
            base, "POST", "/v1/envs/ghost-env/qualify", body={})
        self.assertEqual(status, 404)

    def test_guide_submit_roundtrip_no_execution(self):
        registry = self._register(qualify=True)
        from ghostlab.envs.manifest import import_manifest

        imported = import_manifest(
            registry, json.dumps(valid_manifest()).encode())
        base = self._serve()
        submitted = default_effective()
        submitted["env"]["manifestDigest"] = imported["manifest_digest"]
        status, parsed, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": submitted})
        self.assertEqual(status, 200, parsed)
        self.assertEqual(set(parsed),
                         {"project_id", "requested_id", "resolved_id",
                          "resolved", "mapping_notes"})
        resolved = parsed["resolved"]
        effective = resolved["effective"]
        self.assertEqual(effective["agent"], submitted["agent"]["adapter"])
        self.assertEqual(effective["fixture"],
                         {"order_id": submitted["fixture"]["orderId"],
                          "customer_id": submitted["fixture"]["customerId"],
                          "paid_minor": submitted["fixture"]["paidMinor"],
                          "currency": submitted["fixture"]["currency"]})
        self.assertEqual(effective["faults"],
                         [{"hook": "timeout_after_commit", "params": {},
                           "required": True}])
        self.assertEqual(effective["rules"],
                         submitted["checks"]["invariants"])
        self.assertEqual(effective["limits"]["max_tool_calls"],
                         submitted["limits"]["maxToolCalls"])
        self.assertEqual(effective["limits"]["max_wall_clock_ms"],
                         submitted["limits"]["maxWallClockMs"])
        self.assertEqual(effective["limits"]["max_total_tool_calls"],
                         submitted["limits"]["maxToolCalls"])
        self.assertEqual(effective["repetitions"], {"count": 1})
        self.assertEqual(effective["seeds"], [0])
        self.assertEqual(resolved["pins"]["manifest_digest"],
                         imported["manifest_digest"])
        paths = {note["effective_path"]
                 for note in parsed["mapping_notes"]}
        for required in (
                "schemaVersion", "env.envId", "env.envVersion",
                "env.adapterRef", "env.manifestDigest", "agent.adapter",
                "agent.note", "fixture.orderId", "fixture.customerId",
                "fixture.paidMinor", "fixture.currency",
                "faults.timeoutAfterCommit", "checks.invariants",
                "limits.maxToolCalls", "limits.maxModelCalls",
                "limits.maxInputTokens", "limits.maxOutputTokens",
                "limits.maxWallClockMs", "limits.maxParallelRuns",
                "limits.maxCostMinor", "limits.costCurrency",
                "objective", "successCriteria",
                "(repetitions)", "(policy)"):
            self.assertIn(required, paths)
        store = ExperimentStore(self.store_path)
        self.assertEqual(store.list_runs("proj-e4"), [])

        status, repeat, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": submitted})
        self.assertEqual(status, 200)
        self.assertEqual(repeat["requested_id"], parsed["requested_id"])
        self.assertEqual(repeat["resolved_id"], parsed["resolved_id"])

    def test_guide_unmappable_and_secrets(self):
        self._register(qualify=True)
        base = self._serve()

        tiny = default_effective()
        tiny["limits"]["maxToolCalls"] = 1
        status, parsed, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": tiny})
        self.assertEqual(status, 400)
        self.assertEqual(set(parsed), {"error", "detail"})

        slow = default_effective()
        slow["limits"]["maxWallClockMs"] = 5000000
        status, parsed, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": slow})
        self.assertEqual(status, 400)

        extra = default_effective()
        extra["bogus"] = 1
        status, parsed, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": extra})
        self.assertEqual(status, 400)

        sneaky = default_effective()
        sneaky["agent"]["note"] = "MODEL_API_KEY=" + CANARY
        status, parsed, text = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": sneaky})
        self.assertEqual(status, 400)
        self.assertNotIn(CANARY, text)

        drifted = default_effective()
        drifted["env"]["envVersion"] = "refund-env/999"
        status, parsed, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": drifted})
        self.assertEqual(status, 400)

        unknown_digest = default_effective()
        unknown_digest["env"]["manifestDigest"] = "0" * 64
        status, parsed, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": unknown_digest})
        self.assertEqual(status, 404)

    def test_runs_execute_poll_and_export(self):
        self._register(qualify=True)
        base = self._serve()
        submitted = default_effective()
        status, guide, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": submitted})
        self.assertEqual(status, 200, guide)
        resolved_id = guide["resolved_id"]

        status, started, _ = self._call(
            base, "POST", "/v1/runs", body={"resolved_id": resolved_id})
        self.assertEqual(status, 200, started)
        group = self._poll_group(base, started["group_id"])
        self.assertEqual(group["status"], "completed")
        self.assertEqual(len(group["runs"]), 1)
        run = group["runs"][0]["run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verdict"], "fail")
        self.assertEqual(run["model_calls"], 0)
        self.assertEqual(run["tool_calls"], 3)
        self.assertEqual(run["evidence"]["snapshot"]
                         ["total_refunded_minor"], 500000)

        status, bundle, _ = self._call(
            base, "GET", f"/v1/runs/{started['group_id']}/export")
        self.assertEqual(status, 200, bundle)
        self.assertEqual(bundle["effective_config"], submitted)
        self.assertEqual(bundle["resolved_id"], resolved_id)
        self.assertEqual(bundle["digests"]["effective_digest"],
                         canonical_digest(submitted))
        self.assertEqual(bundle["digests"]["resolved_digest"],
                         guide["resolved"]["digest"])
        self.assertEqual(bundle["digests"]["run_digests"],
                         [group["runs"][0]["digest"]])

        repaired = default_effective()
        repaired["agent"]["adapter"] = "repaired"
        status, guide, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": repaired})
        self.assertEqual(status, 200)
        status, started, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": guide["resolved_id"]})
        self.assertEqual(status, 200)
        group = self._poll_group(base, started["group_id"])
        self.assertEqual(group["runs"][0]["run"]["verdict"], "pass")
        self.assertEqual(group["runs"][0]["run"]["evidence"]["snapshot"]
                         ["total_refunded_minor"], 250000)

    def test_runs_attempt_auto_and_explicit(self):
        self._register(qualify=True)
        base = self._serve()
        status, guide, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": default_effective()})
        self.assertEqual(status, 200, guide)
        resolved_id = guide["resolved_id"]

        # First group auto-assigns attempt 0; re-running the identical
        # config auto-assigns attempt 1 with distinct run ids.
        status, first, _ = self._call(
            base, "POST", "/v1/runs", body={"resolved_id": resolved_id})
        self.assertEqual(status, 200, first)
        self.assertEqual(first["attempt"], 0)
        group = self._poll_group(base, first["group_id"])
        self.assertEqual(group["status"], "completed")
        self.assertEqual(group["attempt"], 0)
        first_ids = [record["run_id"] for record in group["runs"]]

        status, second, _ = self._call(
            base, "POST", "/v1/runs", body={"resolved_id": resolved_id})
        self.assertEqual(status, 200, second)
        self.assertEqual(second["attempt"], 1)
        group = self._poll_group(base, second["group_id"])
        self.assertEqual(group["status"], "completed")
        self.assertEqual(group["attempt"], 1)
        second_ids = [record["run_id"] for record in group["runs"]]
        self.assertEqual(len(second_ids), 1)
        self.assertNotEqual(first_ids, second_ids)
        self.assertEqual(group["runs"][0]["attempt"], 1)

        # Explicit attempts work; collisions fail honestly in the group.
        status, third, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": resolved_id, "attempt": 5})
        self.assertEqual(status, 200, third)
        self.assertEqual(third["attempt"], 5)
        group = self._poll_group(base, third["group_id"])
        self.assertEqual(group["status"], "completed")

        status, fourth, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": resolved_id, "attempt": 0})
        self.assertEqual(status, 200, fourth)
        group = self._poll_group(base, fourth["group_id"])
        self.assertEqual(group["status"], "failed")
        self.assertIn("already recorded", group["error"])

        # Invalid attempts are rejected synchronously.
        for bad in (-1, "1", 1.5, True):
            status, parsed, _ = self._call(
                base, "POST", "/v1/runs",
                body={"resolved_id": resolved_id, "attempt": bad})
            self.assertEqual(status, 400, parsed)

    def test_runs_budget_409_cancel_and_cross_project(self):
        registry = self._register(qualify=True)
        store = ExperimentStore(self.store_path)
        obj = valid_requested(
            limits={"max_tool_calls": 2, "max_total_tool_calls": 4,
                    "max_wall_clock_ms": 60000},
            repetitions={"count": 2})
        created = ops.create_requested_config(store, "proj-e4", obj,
                                              valid_policy(),
                                              "local-operator")
        resolved = ops.resolve_config(store, registry, "proj-e4",
                                      created["config_id"], valid_policy(),
                                      "local-operator")
        base = self._serve()
        status, started, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": resolved["resolved_id"]})
        self.assertEqual(status, 200, started)
        group = self._poll_group(base, started["group_id"])
        self.assertEqual(group["status"], "completed")
        self.assertEqual(len(group["runs"]), 2)
        status, parsed, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": resolved["resolved_id"]})
        self.assertEqual(status, 409)
        self.assertEqual(set(parsed), {"error", "detail"})

        submitted = default_effective()
        status, guide, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": submitted})
        self.assertEqual(status, 200)
        status, started, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": guide["resolved_id"]})
        self.assertEqual(status, 200)
        status, cancelled, _ = self._call(
            base, "POST", f"/v1/runs/{started['group_id']}/cancel")
        self.assertEqual(status, 200)
        self.assertIn("cancelled", cancelled)
        terminal = self._poll_group(base, started["group_id"])
        self.assertIn(terminal["status"], ("cancelled", "completed"))
        if terminal["status"] == "cancelled":
            self.assertEqual(terminal["runs"][0]["run"]["status"],
                             "cancelled")
            self.assertEqual(terminal["runs"][0]["run"]["tool_calls"], 0)
        status, again, _ = self._call(
            base, "POST", f"/v1/runs/{started['group_id']}/cancel")
        self.assertEqual(status, 200)
        self.assertFalse(again["cancelled"])

        other = default_effective()
        status, guide, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": other}, project="proj-a")
        self.assertEqual(status, 200)
        status, parsed, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": guide["resolved_id"]}, project="proj-b")
        self.assertEqual(status, 403)
        status, started, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": guide["resolved_id"]}, project="proj-a")
        self.assertEqual(status, 200)
        for method, path in (
                ("GET", f"/v1/runs/{started['group_id']}"),
                ("POST", f"/v1/runs/{started['group_id']}/cancel"),
                ("GET", f"/v1/runs/{started['group_id']}/export")):
            status, parsed, _ = self._call(
                base, method, path, project="proj-b")
            self.assertEqual(status, 403, (method, path, parsed))
        self._poll_group(base, started["group_id"], project="proj-a")

        for method, path in (
                ("GET", "/v1/runs/grp-missing"),
                ("POST", "/v1/runs/grp-missing/cancel"),
                ("GET", "/v1/runs/grp-missing/export")):
            status, parsed, _ = self._call(base, method, path)
            self.assertEqual(status, 404, (method, path))

    def test_compare_and_errors(self):
        self._register(qualify=True)
        base = self._serve()
        left = default_effective()
        right = default_effective()
        right["agent"]["adapter"] = "repaired"
        status, first, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": left})
        self.assertEqual(status, 200)
        status, second, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": right})
        self.assertEqual(status, 200)
        status, compared, _ = self._call(
            base, "POST", "/v1/compare",
            body={"left_id": first["resolved_id"],
                  "right_id": second["resolved_id"]})
        self.assertEqual(status, 200, compared)
        self.assertFalse(compared["equal"])
        paths = {item["path"] for item in compared["diffs"]}
        self.assertIn("effective.agent", paths)

        status, same, _ = self._call(
            base, "POST", "/v1/compare",
            body={"left_id": first["resolved_id"],
                  "right_id": first["resolved_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(same["equal"])

        status, parsed, _ = self._call(
            base, "POST", "/v1/compare",
            body={"left_id": "res-missing",
                  "right_id": first["resolved_id"]})
        self.assertEqual(status, 404)

        status, parsed, _ = self._call(
            base, "POST", "/v1/compare",
            body={"left_id": first["resolved_id"],
                  "right_id": second["resolved_id"]},
            project="proj-b")
        self.assertEqual(status, 403)

        status, parsed, _ = self._call(
            base, "POST", "/v1/compare",
            body={"left_id": first["resolved_id"]})
        self.assertEqual(status, 400)

    def test_body_cap_json_and_no_registration(self):
        self._register(qualify=True)
        base = self._serve()
        huge = default_effective()
        huge["objective"] = "x" * 1100000
        status, parsed, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": huge})
        self.assertEqual(status, 413)
        self.assertEqual(set(parsed), {"error", "detail"})

        status, parsed, _ = self._call(
            base, "POST", "/v1/guide/submit", raw=b"{nope")
        self.assertEqual(status, 400)

        status, parsed, _ = self._call(
            base, "POST", "/v1/guide/submit", raw=b"{}",
            content_type="text/plain")
        self.assertEqual(status, 400)

        status, _, _ = self._call(base, "POST", "/v1/envs/register",
                                  body={"env_id": "x"})
        self.assertEqual(status, 404)
        status, _, _ = self._call(base, "GET", "/v1/nope")
        self.assertEqual(status, 404)

    def test_paths_configurable_and_node_toolchain(self):
        app = create_app(registry_path=self.registry_path,
                         store_path=self.store_path)
        self.assertEqual(str(app.state.registry.db_path),
                         self.registry_path)
        self.assertEqual(str(app.state.store.db_path), self.store_path)

        custom_reg = str(Path(self.tmp.name) / "custom-env.sqlite")
        custom_store = str(Path(self.tmp.name) / "custom-exp.sqlite")
        previous = (os.environ.get("GHOSTLAB_REGISTRY_DB"),
                    os.environ.get("GHOSTLAB_STORE_DB"))
        os.environ["GHOSTLAB_REGISTRY_DB"] = custom_reg
        os.environ["GHOSTLAB_STORE_DB"] = custom_store
        try:
            via_env = create_app()
        finally:
            if previous[0] is None:
                os.environ.pop("GHOSTLAB_REGISTRY_DB", None)
            else:
                os.environ["GHOSTLAB_REGISTRY_DB"] = previous[0]
            if previous[1] is None:
                os.environ.pop("GHOSTLAB_STORE_DB", None)
            else:
                os.environ["GHOSTLAB_STORE_DB"] = previous[1]
        self.assertEqual(str(via_env.state.registry.db_path), custom_reg)
        self.assertEqual(str(via_env.state.store.db_path), custom_store)

        saved = (os.environ.pop("GHOSTLAB_REGISTRY_DB", None),
                 os.environ.pop("GHOSTLAB_STORE_DB", None))
        try:
            reg_default, store_default = default_paths()
        finally:
            if saved[0] is not None:
                os.environ["GHOSTLAB_REGISTRY_DB"] = saved[0]
            if saved[1] is not None:
                os.environ["GHOSTLAB_STORE_DB"] = saved[1]
        self.assertTrue(reg_default.replace("\\", "/").endswith(
            "artifacts/local/ghostlab-env.sqlite"))
        self.assertTrue(store_default.replace("\\", "/").endswith(
            "artifacts/local/ghostlab-experiments.sqlite"))
        repo = Path(__file__).resolve().parents[1]
        self.assertIn("artifacts/local/",
                      (repo / ".gitignore").read_text())

        try:
            proc = subprocess.run(
                ["node", "--version"], capture_output=True, text=True,
                timeout=15, cwd=str(repo))
        except (OSError, subprocess.TimeoutExpired) as error:
            self.skipTest(f"node unavailable ({error}); TS/browser"
                          " verification is prior-evidence-only")
            return
        if proc.returncode != 0:
            self.skipTest("node --version failed; TS/browser verification"
                          " is prior-evidence-only")
            return
        tsc = repo / "web" / "node_modules" / "typescript" / "bin" / "tsc"
        if not tsc.exists():
            print("tsc not installed; TS verification is"
                  " prior-evidence-only")
            return
        completed = subprocess.run(
            [sys.executable if False else "node", str(tsc), "--noEmit",
             "-p", str(repo / "web" / "tsconfig.app.json")],
            capture_output=True, text=True, timeout=180, cwd=str(repo))
        self.assertEqual(
            completed.returncode, 0,
            f"tsc failed:\n{completed.stdout}\n{completed.stderr}")
        print(f"node {proc.stdout.strip()}: tsc clean; vite build and"
              " browser acceptance not run here (prior-evidence-only:"
              " vite writes dist, acceptance needs Chrome)")


if __name__ == "__main__":
    unittest.main()
