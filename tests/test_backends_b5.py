"""Track B5: backend status routes + run/job status surface.

Drives the new routes over real HTTP (127.0.0.1, ephemeral port,
urllib) against track-private temp DBs. Scans every response body for
secret patterns. No model calls, no secrets, no live inference.
"""

import copy
import gc
import json
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ghostlab.api.app import create_app
from ghostlab.envs.adapters import REFUND_MODES, REFUND_TOOLS
from ghostlab.envs.registry import EnvRegistry
from ghostlab.experiments.configs import scan_for_secrets
from ghostlab.experiments.store import ExperimentStore

REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"
CANARY = "B5-CANARY-4f1e9a02-do-not-echo"

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
        raise RuntimeError("B5 test server did not start")

    def shutdown():
        server.should_exit = True
        thread.join(timeout=15)

    return base, shutdown


class B5Case(unittest.TestCase):
    def setUp(self):
        tmp_base = tempfile.gettempdir()
        self.tmp = tempfile.TemporaryDirectory(prefix="b5-", dir=tmp_base)
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

    def _register(self):
        registry = EnvRegistry(self.registry_path)
        registry.register(
            env_id="refund-env", env_version="refund-env/1",
            adapter_ref=REFUND_REF, display_name="Synthetic refund",
            declared_tools=copy.deepcopy(REFUND_TOOLS),
            declared_modes=list(REFUND_MODES))
        from ghostlab.envs.qualification import run_qualification

        run_qualification(registry, "refund-env")
        return registry

    def _serve(self):
        app = create_app(registry_path=self.registry_path,
                         store_path=self.store_path)
        base, shutdown = start_server(app)
        self._shutdown = shutdown
        return base

    def _call(self, base, method, path, body=None, project="proj-b5"):
        headers = {}
        if project is not None:
            headers["X-GhostLab-Project"] = project
        data = None
        if body is not None:
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

    def _poll_group(self, base, group_id, project="proj-b5", timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, parsed, _ = self._call(
                base, "GET", f"/v1/runs/{group_id}", project=project)
            self.assertEqual(status, 200, parsed)
            if parsed["status"] != "running":
                return parsed
            time.sleep(0.1)
        self.fail(f"group {group_id} still running after {timeout}s")

    def _submit(self, base, project="proj-b5"):
        status, guide, _ = self._call(
            base, "POST", "/v1/guide/submit",
            body={"effective_config": default_effective()},
            project=project)
        self.assertEqual(status, 200, guide)
        return guide["resolved_id"]

    def test_backends_list_shape_and_scoping(self):
        self._register()
        base = self._serve()
        status, parsed, _ = self._call(base, "GET", "/v1/backends")
        self.assertEqual(status, 200, parsed)
        self.assertEqual(parsed["project_id"], "proj-b5")
        self.assertIs(type(parsed["backends"]), list)
        self.assertGreaterEqual(len(parsed["backends"]), 1)
        by_name = {item["name"]: item for item in parsed["backends"]}
        self.assertIn("trusted-local", by_name)
        trusted = by_name["trusted-local"]
        for key in ("name", "version", "trusted_local", "isolation",
                    "isolation_scope", "limitations", "available",
                    "capabilities", "qualification", "limits",
                    "package_identity"):
            self.assertIn(key, trusted)
        self.assertTrue(trusted["trusted_local"])
        self.assertTrue(trusted["available"])
        self.assertIsNone(trusted["unavailable_reason"])
        self.assertIn("in-process", trusted["isolation"])
        self.assertFalse(
            trusted["qualification"]["qualified_for_isolation"])
        self.assertIn("capabilities", trusted)
        self.assertTrue(trusted["capabilities"]["execute_runs"])
        scan_for_secrets(parsed, "backends list")

        # Missing project header refuses; another project gets its echo.
        status, parsed, _ = self._call(base, "GET", "/v1/backends",
                                       project=None)
        self.assertEqual(status, 400)
        self.assertEqual(set(parsed), {"error", "detail"})
        status, other, _ = self._call(base, "GET", "/v1/backends",
                                      project="proj-other")
        self.assertEqual(status, 200)
        self.assertEqual(other["project_id"], "proj-other")

    def test_backend_status_shape_liveness_and_unknown(self):
        self._register()
        base = self._serve()
        status, parsed, _ = self._call(
            base, "GET", "/v1/backends/trusted-local/status")
        self.assertEqual(status, 200, parsed)
        self.assertEqual(parsed["project_id"], "proj-b5")
        backend = parsed["backend"]
        self.assertEqual(backend["name"], "trusted-local")
        self.assertTrue(backend["trusted_local"])
        self.assertIn("capabilities", backend)
        self.assertIn("qualification", backend)
        liveness = parsed["live_liveness"]
        for key in ("backend", "reachable", "checked_at", "detail"):
            self.assertIn(key, liveness)
        self.assertEqual(liveness["backend"], "trusted-local")
        self.assertTrue(liveness["reachable"])
        scan_for_secrets(parsed, "backend status")

        # The known-but-unavailable backend reports status honestly.
        status, down, _ = self._call(
            base, "GET", "/v1/backends/linux-container/status")
        self.assertEqual(status, 200, down)
        self.assertFalse(down["backend"]["available"])
        self.assertIsNotNone(down["backend"]["unavailable_reason"])
        self.assertIn("reachable", down["live_liveness"])
        scan_for_secrets(down, "container backend status")

        status, parsed, _ = self._call(
            base, "GET", "/v1/backends/ghost-backend/status")
        self.assertEqual(status, 404)
        self.assertEqual(set(parsed), {"error", "detail"})

        status, parsed, _ = self._call(
            base, "GET", "/v1/backends/trusted-local/status",
            project=None)
        self.assertEqual(status, 400)
        self.assertEqual(set(parsed), {"error", "detail"})

    def test_runs_reject_unavailable_backend_without_records(self):
        self._register()
        base = self._serve()
        resolved_id = self._submit(base)
        status, parsed, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": resolved_id,
                  "backend": "linux-container"})
        self.assertEqual(status, 503, parsed)
        self.assertEqual(set(parsed), {"error", "detail"})
        self.assertEqual(parsed["error"], "backend_unavailable")
        self.assertIn("linux-container", parsed["detail"])
        # No run record was created anywhere for this refusal.
        store = ExperimentStore(self.store_path)
        self.assertEqual(store.list_runs("proj-b5"), [])
        self.assertEqual(store.list_runs("proj-b5", resolved_id), [])
        scan_for_secrets(parsed, "503 refusal")

        status, parsed, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": resolved_id,
                  "backend": "ghost-backend"})
        self.assertEqual(status, 404)
        self.assertEqual(set(parsed), {"error", "detail"})
        self.assertEqual(store.list_runs("proj-b5"), [])

        status, parsed, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": resolved_id, "backend": 7})
        self.assertEqual(status, 400)
        self.assertEqual(set(parsed), {"error", "detail"})

    def test_trusted_local_run_responses_carry_backend_job_fields(self):
        self._register()
        base = self._serve()
        resolved_id = self._submit(base)

        # Default selection is trusted-local; explicit selection matches.
        for body in ({"resolved_id": resolved_id},
                     {"resolved_id": resolved_id,
                      "backend": "trusted-local"}):
            status, started, _ = self._call(base, "POST", "/v1/runs",
                                            body=body)
            self.assertEqual(status, 200, started)
            self.assertEqual(started["backend"]["name"], "trusted-local")
            self.assertTrue(started["backend"]["trusted_local"])
            self.assertIn("in-process", started["backend"]["isolation"])
            self.assertRegex(started["job"]["job_id"], r"^job-[0-9a-f]+$")
            self.assertEqual(started["job"]["state"], "starting")
            self.assertIsNone(started["termination"])
            scan_for_secrets(started, "run start")

            group = self._poll_group(base, started["group_id"])
            self.assertEqual(group["status"], "completed")
            self.assertEqual(group["backend"]["name"], "trusted-local")
            self.assertEqual(group["job"]["job_id"],
                             started["job"]["job_id"])
            self.assertEqual(group["job"]["state"], "completed")
            self.assertEqual(group["termination"]["reason"], "completed")
            self.assertEqual(len(group["runs"]), 1)
            run = group["runs"][0]["run"]
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["model_calls"], 0)
            scan_for_secrets(group, "run group")

            status, bundle, _ = self._call(
                base, "GET", f"/v1/runs/{started['group_id']}/export")
            self.assertEqual(status, 200, bundle)
            self.assertEqual(bundle["backend"]["name"], "trusted-local")
            self.assertEqual(bundle["job"]["job_id"],
                             started["job"]["job_id"])
            self.assertEqual(bundle["termination"]["reason"],
                             "completed")
            scan_for_secrets(bundle, "run export")

    def test_cross_project_scoping_with_backend_fields(self):
        self._register()
        base = self._serve()
        resolved_id = self._submit(base, project="proj-a")
        status, started, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": resolved_id}, project="proj-a")
        self.assertEqual(status, 200, started)
        group_id = started["group_id"]
        self._poll_group(base, group_id, project="proj-a")

        for method, path in (
                ("GET", f"/v1/runs/{group_id}"),
                ("POST", f"/v1/runs/{group_id}/cancel"),
                ("GET", f"/v1/runs/{group_id}/export")):
            status, parsed, _ = self._call(base, method, path,
                                           body={} if method == "POST"
                                           else None,
                                           project="proj-b")
            self.assertEqual(status, 403, (method, path, parsed))
            self.assertEqual(set(parsed), {"error", "detail"})

        # Backend status is operator-global but project-scoped in the
        # response: it echoes the caller and leaks no run records.
        status, parsed, text = self._call(base, "GET", "/v1/backends",
                                          project="proj-b")
        self.assertEqual(status, 200)
        self.assertEqual(parsed["project_id"], "proj-b")
        self.assertNotIn(group_id, text)
        self.assertNotIn(resolved_id, text)

    def test_new_responses_carry_no_secrets(self):
        self._register()
        base = self._serve()
        payloads = []
        status, listed, _ = self._call(base, "GET", "/v1/backends")
        self.assertEqual(status, 200)
        payloads.append(("backends", listed))
        for item in listed["backends"]:
            status, one, _ = self._call(
                base, "GET", f"/v1/backends/{item['name']}/status")
            self.assertEqual(status, 200)
            payloads.append((item["name"], one))
        resolved_id = self._submit(base)
        status, refused, _ = self._call(
            base, "POST", "/v1/runs",
            body={"resolved_id": resolved_id,
                  "backend": "linux-container"})
        self.assertEqual(status, 503)
        payloads.append(("refused", refused))
        for label, payload in payloads:
            scan_for_secrets(payload, f"B5 {label}")
            text = json.dumps(payload)
            for pattern in SECRET_PATTERNS:
                self.assertIsNone(pattern.search(text),
                                  f"{pattern.pattern} in {label}")
            self.assertNotIn(CANARY, text)


if __name__ == "__main__":
    unittest.main()
