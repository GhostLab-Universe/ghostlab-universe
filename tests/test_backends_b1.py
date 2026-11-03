import gzip
import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile

from ghostlab.backends import (BackendError, JobSpec, LocalBackend,
                               ProtocolError)
from ghostlab.backends import intake as intake_mod
from ghostlab.backends import protocol as protocol_mod

ADAPTER_REF = "ghostlab.envs.adapters:RefundEnvAdapter"
FIXTURE = {"order_id": "synthetic-order-1",
           "customer_id": "synthetic-customer-1",
           "paid_minor": 250000, "currency": "INR"}
PACKAGE = {"kind": "checked-in", "identity": "test-digest-b1"}
LIMITS = {"wall_clock_ms": 30000, "max_output_bytes": 1000000}


def make_spec(job_id, **over):
    args = {"job_id": job_id, "project_id": "proj-b1",
            "package": dict(PACKAGE), "adapter_ref": ADAPTER_REF,
            "fixture": dict(FIXTURE), "limits": dict(LIMITS)}
    args.update(over)
    return JobSpec(**args)


def poll_until(backend, job_id, want_types, timeout_s=20.0):
    if isinstance(want_types, str):
        want_types = {want_types}
    else:
        want_types = set(want_types)
    collected = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for event in backend.poll(job_id, 500):
            collected.append(event)
            if event["type"] in want_types:
                return collected
        state = backend.status(job_id)["state"]
        if state in ("completed", "failed", "cancelled", "timed_out"):
            for event in backend.poll(job_id, 0):
                collected.append(event)
                if event["type"] in want_types:
                    return collected
            return collected
    return collected


def make_tar(entries):
    """entries: list of (name, bytes) or ("link:", name, target)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for entry in entries:
            if entry[0] == "link:":
                info = tarfile.TarInfo(entry[1])
                info.type = tarfile.SYMTYPE
                info.linkname = entry[2]
                archive.addfile(info)
                continue
            name, data = entry
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def make_zip(entries, symlink_names=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            if name in symlink_names:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, data)
            else:
                archive.writestr(name, data)
    return buffer.getvalue()


class ProtocolTests(unittest.TestCase):
    def test_roundtrip(self):
        raw = protocol_mod.encode_message(
            "job-1", 0, "invoke", {"tool": "get_order"})
        message = protocol_mod.decode_message(raw)
        self.assertEqual(message, {"v": 1, "job_id": "job-1", "seq": 0,
                                   "type": "invoke",
                                   "payload": {"tool": "get_order"}})
        self.assertEqual(protocol_mod.decode_message(
            raw, expected_job="job-1", expected_seq=0)["seq"], 0)

    def test_oversize_rejected(self):
        with self.assertRaises(ProtocolError):
            protocol_mod.encode_message("job-1", 0, "evidence_chunk",
                                        {"blob": "x" * 70000})
        with self.assertRaises(ProtocolError):
            protocol_mod.decode_message(b"x" * 70000)
        with self.assertRaises(ProtocolError):
            protocol_mod.decode_message(
                b'{"v":1,"job_id":"j","seq":0,"type":"t","payload":{}}'
                + b" " * 70000)

    def test_depth_rejected(self):
        ok = {}
        for _ in range(7):
            ok = {"k": ok}
        protocol_mod.encode_message("job-1", 0, "t", ok)
        deep = {}
        for _ in range(9):
            deep = {"k": deep}
        with self.assertRaises(ProtocolError):
            protocol_mod.encode_message("job-1", 0, "t", deep)
        raw = json.dumps({"v": 1, "job_id": "j", "seq": 0, "type": "t",
                          "payload": deep}).encode()
        with self.assertRaises(ProtocolError):
            protocol_mod.decode_message(raw)

    def test_wrong_types_rejected(self):
        base = {"v": 1, "job_id": "j", "seq": 0, "type": "t",
                "payload": {}}
        variants = [
            {**base, "v": "1"},
            {**base, "v": True},
            {**base, "job_id": 123},
            {**base, "job_id": ""},
            {**base, "seq": True},
            {**base, "seq": -1},
            {**base, "seq": "0"},
            {**base, "type": ""},
            {**base, "payload": []},
            {k: v for k, v in base.items() if k != "seq"},
            {**base, "extra": 1},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(ProtocolError):
                    protocol_mod.decode_message(
                        json.dumps(variant).encode())
        with self.assertRaises(ProtocolError):
            protocol_mod.decode_message(b"not json")
        with self.assertRaises(ProtocolError):
            protocol_mod.decode_message(
                b'{"v":1,"job_id":"j","seq":0,"type":"t","payload":NaN}')
        with self.assertRaises(ProtocolError):
            protocol_mod.decode_message(12345)
        with self.assertRaises(ProtocolError):
            protocol_mod.encode_message("j", 0, "t", {"bad": {1, 2}})

    def test_seq_and_job_checks(self):
        raw = protocol_mod.encode_message("job-1", 3, "t", {})
        with self.assertRaises(ProtocolError):
            protocol_mod.decode_message(raw, expected_seq=5)
        with self.assertRaises(ProtocolError):
            protocol_mod.decode_message(raw, expected_job="other-job")
        self.assertEqual(
            protocol_mod.decode_message(
                raw, expected_job="job-1", expected_seq=3)["seq"], 3)


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b1-intake-")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmp, ignore_errors=True))

    def test_clean_tar_roundtrip(self):
        data = make_tar([("a.txt", b"hello"),
                         ("sub/b.txt", b"world" * 100)])
        manifest = intake_mod.validate_archive(data, "tar.gz", 1 << 20,
                                               100, 64)
        self.assertEqual(len(manifest["files"]), 2)
        dest = os.path.join(self.tmp, "ws1")
        os.makedirs(dest)
        out = intake_mod.extract_archive(data, "tar.gz", dest,
                                         max_bytes=1 << 20, max_files=100,
                                         max_ratio=64)
        self.assertEqual(out["identity"], manifest["identity"])
        with open(os.path.join(dest, "sub", "b.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"world" * 100)

    def test_clean_zip_roundtrip(self):
        data = make_zip([("a.txt", b"hello"), ("sub/b.txt", b"data")])
        manifest = intake_mod.validate_archive(data, "zip", 1 << 20,
                                               100, 64)
        self.assertEqual(len(manifest["files"]), 2)
        dest = os.path.join(self.tmp, "ws2")
        os.makedirs(dest)
        intake_mod.extract_archive(data, "zip", dest, max_bytes=1 << 20,
                                   max_files=100, max_ratio=64)
        with open(os.path.join(dest, "a.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"hello")

    def test_traversal_rejected(self):
        for kind, data in (
                ("tar.gz", make_tar([("../evil.txt", b"x")])),
                ("tar.gz", make_tar([("sub/../../evil.txt", b"x")])),
                ("zip", make_zip([("../../evil.txt", b"x")]))):
            with self.subTest(kind=kind), self.assertRaises(BackendError):
                intake_mod.validate_archive(data, kind, 1 << 20, 100, 64)
        dest = os.path.join(self.tmp, "ws3")
        os.makedirs(dest)
        with self.assertRaises(BackendError):
            intake_mod.extract_archive(
                make_zip([("../evil.txt", b"x")]), "zip", dest,
                max_bytes=1 << 20, max_files=100, max_ratio=64)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "evil.txt")))

    def test_absolute_paths_rejected(self):
        for kind, data in (
                ("tar.gz", make_tar([("/etc/x", b"x")])),
                ("zip", make_zip([("/abs.txt", b"x")])),
                ("zip", make_zip([("C:/win.txt", b"x")]))):
            with self.subTest(kind=kind), self.assertRaises(BackendError):
                intake_mod.validate_archive(data, kind, 1 << 20, 100, 64)

    def test_unsafe_links_rejected(self):
        tar_data = make_tar([("link:", "evil-link", "/etc/passwd")])
        with self.assertRaises(BackendError):
            intake_mod.validate_archive(tar_data, "tar.gz", 1 << 20, 100, 64)
        zip_data = make_zip([("evil-link", "target")],
                            symlink_names=("evil-link",))
        with self.assertRaises(BackendError):
            intake_mod.validate_archive(zip_data, "zip", 1 << 20, 100, 64)

    def test_expansion_bombs_rejected(self):
        many = [(f"f{i}.txt", b"x") for i in range(200)]
        with self.assertRaises(BackendError):
            intake_mod.validate_archive(make_zip(many), "zip", 1 << 20,
                                        100, 64)
        with self.assertRaises(BackendError):
            intake_mod.validate_archive(make_tar(many), "tar.gz", 1 << 20,
                                        100, 64)
        # Highly compressible payload: tiny on the wire, large extracted.
        big = make_zip([("big.bin", b"\x00" * 200000)])
        self.assertLess(len(big), 1024)
        with self.assertRaises(BackendError):
            intake_mod.validate_archive(big, "zip", 1024, 100, 64)
        # Declared-size bomb: header claims 1GB; header scan must reject
        # without reading that much data.
        info = tarfile.TarInfo("huge.bin")
        info.size = 10 ** 9
        header_bomb = gzip.compress(
            info.tobuf(tarfile.GNU_FORMAT, "utf-8", "strict")
            + b"\x00" * 1024)
        with self.assertRaises(BackendError):
            intake_mod.validate_archive(header_bomb, "tar.gz",
                                        1 << 20, 100, 64)

    def test_bad_inputs_rejected(self):
        with self.assertRaises(ValueError):
            intake_mod.validate_archive(b"", "zip", 100, 100, 64)
        with self.assertRaises(ValueError):
            intake_mod.validate_archive(b"x", "rar", 100, 100, 64)
        with self.assertRaises(ValueError):
            intake_mod.validate_archive(b"x", "zip", 0, 100, 64)
        with self.assertRaises(BackendError):
            intake_mod.validate_archive(b"not-an-archive" * 10, "zip",
                                        1 << 20, 100, 64)
        with self.assertRaises(BackendError):
            intake_mod.validate_archive(b"not-an-archive" * 10, "tar.gz",
                                        1 << 20, 100, 64)
        dest = os.path.join(self.tmp, "missing")
        with self.assertRaises(BackendError):
            intake_mod.extract_archive(make_zip([("a.txt", b"x")]), "zip",
                                       dest, max_bytes=1 << 20,
                                       max_files=100, max_ratio=64)


class LocalBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b1-local-")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.tmp, ignore_errors=True))
        self.backend = LocalBackend(os.path.join(self.tmp, "workspaces"))

    def tearDown(self):
        for job_id in list(getattr(self.backend, "_jobs", {})):
            try:
                self.backend.close(job_id)
            except BackendError:
                pass

    def test_capabilities_honest(self):
        caps = self.backend.capabilities()
        self.assertEqual(caps["isolation"],
                         {"filesystem": "none", "process": "os-process",
                          "network": "host"})
        self.assertEqual(caps["qualification"]["status"], "unqualified")
        self.assertIn("wall_clock_ms", caps["limits"])

    def test_spec_validation(self):
        with self.assertRaises(ValueError):
            make_spec("../escape")
        with self.assertRaises(ValueError):
            make_spec("ok", adapter_ref="not-a-ref")
        with self.assertRaises(ValueError):
            make_spec("ok", limits={"wall_clock_ms": 0,
                                    "max_output_bytes": 10})
        with self.assertRaises(ValueError):
            make_spec("ok", limits={"wall_clock_ms": True,
                                    "max_output_bytes": 10})
        with self.assertRaises(ValueError):
            make_spec("ok", package={"kind": "checked-in"})
        with self.assertRaises(ValueError):
            make_spec("ok", fixture=[])

    def test_unknown_job_refused(self):
        with self.assertRaises(BackendError):
            self.backend.send("no-such-job", {"type": "hello",
                                              "payload": {}})
        with self.assertRaises(BackendError):
            self.backend.poll("no-such-job", 0)
        with self.assertRaises(BackendError):
            self.backend.status("no-such-job")
        with self.assertRaises(BackendError):
            self.backend.cancel("no-such-job", 10)
        with self.assertRaises(BackendError):
            self.backend.close("no-such-job")

    def test_prepare_and_happy_path(self):
        prepared = self.backend.prepare(
            {"kind": "checked-in", "identity": "test-digest-b1"})
        self.assertEqual(prepared["identity"], "test-digest-b1")
        with self.assertRaises(BackendError):
            self.backend.prepare({"kind": "docker-image"})
        job_id = "job-happy-1"
        self.backend.start_job(make_spec(job_id))
        with self.assertRaises(BackendError):
            self.backend.start_job(make_spec(job_id))
        self.backend.send(job_id, {"type": "hello", "payload": {}})
        events = poll_until(self.backend, job_id, "started")
        started = next(e for e in events if e["type"] == "started")
        self.assertEqual(started["payload"]["adapter"], ADAPTER_REF)
        self.assertEqual(started["payload"]["describe"]["env_id"],
                         "refund-env")
        self.assertEqual(self.backend.status(job_id)["state"], "running")

        self.backend.send(job_id, {"type": "reset",
                                   "payload": {"fixture": FIXTURE}})
        events = poll_until(self.backend, job_id, "tool_result")
        self.assertTrue(events[-1]["payload"]["ok"])

        identity = {"order_id": FIXTURE["order_id"],
                    "customer_id": FIXTURE["customer_id"]}
        self.backend.send(job_id, {"type": "invoke",
                                   "payload": {"tool": "get_order",
                                               "arguments": identity}})
        events = poll_until(self.backend, job_id, "tool_result")
        result = events[-1]["payload"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["order_id"],
                         FIXTURE["order_id"])

        self.backend.send(job_id, {
            "type": "invoke",
            "payload": {"tool": "issue_refund",
                        "arguments": {**identity, "amount_minor": 250000,
                                      "idempotency_key": "b1-key-1"}}})
        events = poll_until(self.backend, job_id, "tool_result")
        refund = events[-1]["payload"]
        self.assertTrue(refund["ok"])
        self.assertEqual(refund["result"]["transaction_id"], "refund-1")

        self.backend.send(job_id, {"type": "snapshot", "payload": {}})
        events = poll_until(self.backend, job_id, "tool_result")
        snap = events[-1]["payload"]["result"]
        self.assertEqual(len(snap["ledger"]), 1)

        self.backend.send(job_id, {"type": "set_fault",
                                   "payload": {"name": "nope",
                                               "params": {}}})
        events = poll_until(self.backend, job_id, "tool_result")
        self.assertFalse(events[-1]["payload"]["ok"])

        self.backend.send(job_id, {"type": "evidence", "payload": {}})
        events = poll_until(self.backend, job_id, "evidence_chunk")
        chunk = next(e for e in events if e["type"] == "evidence_chunk")
        self.assertIn("trace", chunk["payload"]["evidence"])
        self.assertGreaterEqual(
            len(chunk["payload"]["evidence"]["trace"]), 2)

        self.backend.send(job_id, {"type": "close", "payload": {}})
        events = poll_until(self.backend, job_id, "completed")
        self.assertTrue(any(e["type"] == "completed" for e in events))
        with self.assertRaises(BackendError):
            self.backend.send(job_id, {"type": "hello", "payload": {}})
        workspace = os.path.join(self.tmp, "workspaces", job_id)
        self.assertTrue(os.path.isdir(workspace))
        receipt = self.backend.close(job_id)
        self.assertEqual(receipt["state"], "completed")
        kinds = {r["kind"] for r in receipt["receipts"]}
        self.assertIn("lifecycle", kinds)
        self.assertIn("output", kinds)
        self.assertFalse(os.path.exists(workspace))
        with self.assertRaises(BackendError):
            self.backend.status(job_id)

    def test_archive_package_start(self):
        data = make_zip([("notes.txt", b"reviewed notes")])
        prepared = self.backend.prepare({"kind": "zip", "data": data})
        job_id = "job-arch-1"
        spec = make_spec(job_id, package={"kind": "zip",
                                          "identity": prepared["identity"]})
        self.backend.start_job(spec)
        try:
            self.assertTrue(os.path.isfile(os.path.join(
                self.tmp, "workspaces", job_id, "notes.txt")))
            self.backend.send(job_id, {"type": "hello", "payload": {}})
            events = poll_until(self.backend, job_id, "started")
            self.assertTrue(any(e["type"] == "started" for e in events))
            self.backend.send(job_id, {"type": "close", "payload": {}})
            poll_until(self.backend, job_id, "completed")
        finally:
            self.backend.close(job_id)
        with self.assertRaises(BackendError):
            self.backend.start_job(make_spec(
                "job-arch-2",
                package={"kind": "zip", "identity": "0" * 64}))

    def test_cancel_terminates_and_confirms(self):
        job_id = "job-cancel-1"
        self.backend.start_job(make_spec(job_id))
        self.backend.send(job_id, {"type": "hello", "payload": {}})
        poll_until(self.backend, job_id, "started")
        outcome = self.backend.cancel(job_id, 3000)
        self.assertEqual(outcome["state"], "cancelled")
        self.assertTrue(outcome["confirmed"])
        proc = self.backend._jobs[job_id].proc
        self.assertIsNotNone(proc.poll())
        self.assertEqual(self.backend.status(job_id)["state"], "cancelled")
        with self.assertRaises(BackendError):
            self.backend.send(job_id, {"type": "hello", "payload": {}})
        receipt = self.backend.close(job_id)
        self.assertEqual(receipt["state"], "cancelled")

    def test_wall_clock_timeout(self):
        job_id = "job-timeout-1"
        spec = make_spec(job_id, limits={"wall_clock_ms": 400,
                                         "max_output_bytes": 1000000})
        self.backend.start_job(spec)
        deadline = time.monotonic() + 20
        state = None
        while time.monotonic() < deadline:
            state = self.backend.status(job_id)["state"]
            if state == "timed_out":
                break
            time.sleep(0.1)
        self.assertEqual(state, "timed_out")
        receipt = self.backend.close(job_id)
        self.assertEqual(receipt["state"], "timed_out")
        reasons = [r.get("reason") for r in receipt["receipts"]]
        self.assertIn("wall-clock-exhausted", reasons)

    def test_output_exhaustion_truncates(self):
        job_id = "job-out-1"
        spec = make_spec(job_id, limits={"wall_clock_ms": 30000,
                                         "max_output_bytes": 200})
        self.backend.start_job(spec)
        self.backend.send(job_id, {"type": "hello", "payload": {}})
        events = poll_until(self.backend, job_id, {"truncated", "failed"})
        self.assertTrue(events)
        self.assertEqual(self.backend.status(job_id)["state"], "failed")
        receipt = self.backend.close(job_id)
        self.assertEqual(receipt["state"], "failed")
        self.assertTrue(any(r.get("truncated") for r in receipt["receipts"]))

    # -- bad-worker shims via the env hook (worker.py untouched) --------

    def _install_shim(self, name, source):
        shim_dir = os.path.join(self.tmp, "shims")
        os.makedirs(shim_dir, exist_ok=True)
        with open(os.path.join(shim_dir, name + ".py"), "w",
                  encoding="utf-8") as handle:
            handle.write(source)
        old_path = os.environ.get("PYTHONPATH")
        old_worker = os.environ.get("GHOSTLAB_BACKEND_WORKER_MODULE")
        new_path = shim_dir + (os.pathsep + old_path if old_path else "")
        os.environ["PYTHONPATH"] = new_path
        os.environ["GHOSTLAB_BACKEND_WORKER_MODULE"] = name

        def _restore():
            if old_path is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = old_path
            if old_worker is None:
                os.environ.pop("GHOSTLAB_BACKEND_WORKER_MODULE", None)
            else:
                os.environ["GHOSTLAB_BACKEND_WORKER_MODULE"] = old_worker
        self.addCleanup(_restore)

    def test_malformed_worker_line_fails(self):
        self._install_shim("bad_shim_b1",
                           "import sys, time\n"
                           "sys.stdout.write('THIS IS NOT JSON\\n')\n"
                           "sys.stdout.flush()\n"
                           "time.sleep(30)\n")
        job_id = "job-malformed-1"
        self.backend.start_job(make_spec(job_id))
        events = poll_until(self.backend, job_id, "failed")
        failed = next(e for e in events if e["type"] == "failed")
        self.assertEqual(failed["payload"]["error"],
                         "malformed-worker-output")
        self.assertEqual(self.backend.status(job_id)["state"], "failed")
        receipt = self.backend.close(job_id)
        self.assertEqual(receipt["state"], "failed")

    def test_env_scrubbing(self):
        self._install_shim(
            "envdump_shim_b1",
            "import json, os, sys\n"
            "job = os.environ['GHOSTLAB_JOB_ID']\n"
            "keys = sorted(os.environ.keys())\n"
            "msg = {'v': 1, 'job_id': job, 'seq': 0,\n"
            "       'type': 'started', 'payload': {'env_keys': keys}}\n"
            "sys.stdout.write(json.dumps(msg) + '\\n')\n"
            "sys.stdout.flush()\n"
            "for _line in sys.stdin:\n"
            "    pass\n")
        os.environ["GHOSTLAB_B1_CANARY"] = "present-in-parent"
        self.addCleanup(lambda: os.environ.pop("GHOSTLAB_B1_CANARY", None))
        job_id = "job-env-1"
        self.backend.start_job(make_spec(job_id))
        try:
            events = poll_until(self.backend, job_id, "started")
            started = next(e for e in events if e["type"] == "started")
            keys = started["payload"]["env_keys"]
            self.assertNotIn("GHOSTLAB_B1_CANARY", keys)
            self.assertIn("GHOSTLAB_JOB_ID", keys)
            self.assertIn("GHOSTLAB_ADAPTER_REF", keys)
            if "PATH" in os.environ:
                self.assertIn("PATH", keys)
            if sys.platform == "win32" and "SYSTEMROOT" in os.environ:
                self.assertIn("SYSTEMROOT", keys)
            allowed_extras = {"PATH", "SYSTEMROOT", "PYTHONPATH",
                              "GHOSTLAB_JOB_ID", "GHOSTLAB_ADAPTER_REF",
                              "GHOSTLAB_ALLOWED_PREFIXES",
                              "GHOSTLAB_MAX_OUTPUT_BYTES"}
            self.assertEqual(set(keys) - allowed_extras, set())
        finally:
            self.backend.close(job_id)


if __name__ == "__main__":
    unittest.main()
