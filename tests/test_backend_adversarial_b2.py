"""TRACK B2: adversarial qualification suite for ExecutionBackend (external verify).

Honesty core: every check carries a per-backend EXPECTATION profile
(``EXPECTED[backend][check]`` in ``{"enforced", "absent"}``). A backend that
claims ``enforced`` but lets the attack through FAILS the suite; a backend
profiled ``absent`` must let the attack OBSERVABLY succeed or the test
fails as vacuous. Never xfail, never silent.

Local profile (reference ``LocalBackend``): protocol-level checks
(oversize/malformed/forged output, forged job ids, env scrub,
no-host-path-write) are ``enforced``; every namespace / filesystem /
network / resource / cancel-survival check is ``absent``-with-proof,
because the local backend is an explicitly UNQUALIFIED reference that
provides no isolation (never presented as a sandbox).

How payloads run: harmless probe attacks execute INSIDE the job as worker
``invoke`` commands against a test-only probe adapter (``ghostlab.b2_probe``;
shipped to the worker through a temp-dir ``ghostlab`` package shim on
``PYTHONPATH`` because B1's worker allowlists ``ghostlab.*``). Verdicts use
EXTERNAL host-side evidence only (files, listener hits, job states):
unguessable random tokens make exfiltration-via-result a sound proof (the
job can only echo a token it actually obtained), and every such channel
carries a positive control.

Scope: disposable temp resources only, no real secrets, no destructive
ops, no model calls. Stdlib only.
"""

import contextlib
import json
import os
import py_compile
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRCS = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRCS) not in sys.path:
    sys.path.insert(0, str(SRCS))

from ghostlab.backends import protocol as _protocol  # noqa: E402
from ghostlab.backends.interface import (BACKEND_VERSION, TERMINAL_STATES,  # noqa: E402
                                         BackendError, JobSpec, ProtocolError)
from ghostlab.backends.local import LocalBackend  # noqa: E402

ENFORCED = "enforced"
ABSENT = "absent"

# Honesty core: profiled expectation per backend per check.
EXPECTED = {
    "local": {
        "host_canary_read": ABSENT,
        "sibling_canary_read": ABSENT,
        "metadata_endpoint": ABSENT,
        "listener_egress": ABSENT,
        "write_outside_workspace": ABSENT,
        "traversal_via_outputs": ABSENT,
        "symlink_escape": ABSENT,
        "fork_burst": ABSENT,
        "cpu_spin": ABSENT,
        "mem_grow": ABSENT,
        "disk_fill": ABSENT,
        "oversize_event": ENFORCED,
        "malformed_event": ENFORCED,
        "cancel_survival": ABSENT,
        "forged_job_id": ENFORCED,
        "env_scrub": ENFORCED,
        "no_host_path_write": ENFORCED,
        "hello_protocol": ENFORCED,
    },
}

# Backend-parameterized registry: local now, docker later (no daemon here).
BACKENDS = {"local": LocalBackend}

PROBE_REF = "ghostlab.b2_probe:B2ProbeAdapter"
BOOKING_REF = "ghostlab.b2_booking:BookingEnvAdapter"
BOOKING_FIXTURE = {
    "equipment_id": "synthetic-microscope-1",
    "user_id": "synthetic-user-1",
    "slot": "2026-11-03T09:00Z",
}

# Temp-dir ``ghostlab`` package shim: extends the package path so the REAL
# src/ghostlab submodules (backends.worker, envs.adapters) keep importing
# while the worker-only test instruments resolve under ghostlab.*.
_SHIM_INIT = r'''"""B2 test-only package shim (worker side, temp dir)."""
from pkgutil import extend_path
__path__ = extend_path(__path__, __name__)
'''

_BOOKING_SHIM = r'''"""B2 booking re-export under the worker allowlist (ghostlab.*)."""
from examples.equipment_booking.env_adapter import BookingEnvAdapter  # noqa: F401
'''

# Standalone stdlib-only probe adapter executed INSIDE the job worker.
_PROBE_SOURCE = r'''"""B2 adversarial probe adapter (TEST INSTRUMENT, job side, temp dir)."""
import os
import socket
import subprocess
import sys
import time


class B2ProbeAdapter:
    env_id = "b2-probe"
    version = "b2-probe/1"

    def __init__(self):
        self._fixture = {}
        self._log = []

    def describe(self):
        return {
            "env_id": self.env_id,
            "version": self.version,
            "tools": [{"name": "attack", "parameters": ["name"]}],
            "reproduction_modes": ["deterministic-reset"],
            "fault_hooks": [],
            "supports_cancellation": False,
            "isolation": "none-test-probe",
        }

    def reset(self, fixture):
        if type(fixture) is not dict:
            raise ValueError("fixture must be an object")
        self._fixture = dict(fixture)

    def _record(self, name):
        self._log.append(name)

    def invoke(self, tool, arguments):
        if tool != "attack" or type(arguments) is not dict:
            raise ValueError("unknown tool")
        name = arguments.get("name")
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError("unknown attack")
        handler = getattr(self, "_attack_" + name, None)
        if handler is None:
            raise ValueError("unknown attack %r" % (name,))
        self._record(name)
        return handler(arguments)

    def invoke_cancellable(self, tool, arguments, cancel=None):
        if cancel is not None and cancel.is_set():
            raise Exception("cancelled")
        return self.invoke(tool, arguments)

    def snapshot(self):
        return {"probe": "b2", "attacks": len(self._log)}

    def evidence(self):
        return {"probe": "b2", "attacks": list(self._log),
                "fixture_keys": sorted(self._fixture)}

    def set_fault(self, name, params):
        return None

    def clear_faults(self):
        return None

    def probe_calls(self, fixture):
        return [{"tool": "attack",
                 "arguments": {"name": "echo", "value": "ok"}}]

    def _attack_echo(self, args):
        return {"value": args.get("value")}

    def _attack_cwd(self, args):
        return {"cwd": os.getcwd()}

    def _attack_read_file(self, args):
        with open(args["path"], "rb") as handle:
            data = handle.read()
        return {"size": len(data),
                "content": data.decode("utf-8", "replace")}

    def _attack_write_file(self, args):
        content = args.get("content", "")
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        parent = os.path.dirname(os.path.abspath(args["path"]))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args["path"], "wb") as handle:
            handle.write(raw)
        return {"size": len(raw)}

    def _attack_tcp_connect(self, args):
        start = time.monotonic()
        sock = socket.create_connection(
            (args["host"], int(args["port"])),
            timeout=float(args.get("timeout_s", 5)))
        try:
            sock.sendall((str(args.get("token", "")) + "\n").encode("utf-8"))
        finally:
            sock.close()
        return {"connected": True,
                "elapsed_ms": int((time.monotonic() - start) * 1000)}

    def _attack_http_metadata(self, args):
        host = args.get("host", "169.254.169.254")
        port = int(args.get("port", 80))
        timeout = float(args.get("timeout_s", 2))
        start = time.monotonic()
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as error:
            outcome = {"outcome": "socket-error", "errno": error.errno,
                       "detail": str(error)[:200]}
        else:
            try:
                sock.sendall(b"GET /latest/meta-data/ HTTP/1.0\r\n"
                             b"Host: metadata\r\n\r\n")
                sock.settimeout(timeout)
                data = sock.recv(4096)
                outcome = {"outcome": "connected", "bytes": len(data)}
            except OSError as error:
                outcome = {"outcome": "io-error", "errno": error.errno,
                           "detail": str(error)[:200]}
            finally:
                sock.close()
        outcome["elapsed_ms"] = int((time.monotonic() - start) * 1000)
        return outcome

    def _attack_spawn_many(self, args):
        count = int(args["count"])
        outdir = args["outdir"]
        token = str(args.get("token", ""))
        os.makedirs(outdir, exist_ok=True)
        script = os.path.join(outdir, "child.py")
        with open(script, "w") as handle:
            handle.write("import sys\n"
                         "open(sys.argv[1], 'w').write(sys.argv[2])\n")
        procs = []
        for index in range(count):
            target = os.path.join(outdir, "child-%d.txt" % index)
            procs.append(subprocess.Popen(
                [sys.executable, script, target, "%s-%d" % (token, index)]))
        codes = []
        deadline = time.monotonic() + float(args.get("timeout_s", 60))
        for proc in procs:
            remaining = max(1.0, deadline - time.monotonic())
            try:
                codes.append(proc.wait(timeout=remaining))
            except subprocess.TimeoutExpired:
                proc.kill()
                codes.append("timeout")
        return {"started": len(procs), "codes": codes}

    def _attack_cpu_spin(self, args):
        seconds = float(args.get("seconds", 3.0))
        wall_start = time.monotonic()
        cpu_start = time.process_time()
        while time.process_time() - cpu_start < seconds:
            pass
        return {"cpu_s": time.process_time() - cpu_start,
                "wall_s": time.monotonic() - wall_start}

    def _attack_mem_grow(self, args):
        size = int(args.get("bytes", 256 * 1024 * 1024))
        block = bytearray(size)
        for offset in range(0, size, 4096):
            block[offset] = 1
        time.sleep(0.5)
        return {"held": len(block)}

    def _attack_disk_fill(self, args):
        size = int(args["bytes"])
        chunk = b"\0" * (1024 * 1024)
        written = 0
        with open(args["path"], "wb") as handle:
            while written < size:
                step = min(len(chunk), size - written)
                handle.write(chunk[:step])
                written += step
        return {"size": written}

    def _attack_symlink_probe(self, args):
        try:
            os.symlink(args["target"], args["link"])
        except OSError as error:
            return {"created": False, "errno": error.errno,
                    "message": str(error)[:200]}
        return {"created": True}

    def _attack_spawn_detached(self, args):
        script = args["script"]
        parent = os.path.dirname(os.path.abspath(script))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(script, "w") as handle:
            handle.write(args["script_text"])
        if os.name == "nt":
            proc = subprocess.Popen(
                [sys.executable, script], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
        else:
            proc = subprocess.Popen(
                [sys.executable, script], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True)
        return {"pid": proc.pid}

    def _attack_env_dump(self, args):
        return {"env": dict(os.environ)}

    def _attack_big_result(self, args):
        return {"blob": "x" * int(args.get("bytes", 1024 * 1024))}
'''

_BADWORKER_TEMPLATE = r'''"""B2 fault-injection worker ({mode}): stdlib only, ignores stdin."""
import os
import sys
import time


def _job():
    return os.environ.get("GHOSTLAB_JOB_ID", "missing")


def main():
{BODY}
    sys.stdout.buffer.flush()
    time.sleep(1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_BADWORKER_BODIES = {
    # garbage bytes: not JSON at all -> malformed-worker-output
    "garbage": '    sys.stdout.buffer.write(b"this is not a protocol line{{{\\n")\n',
    # one 100KB line: exceeds the 64KB frame cap -> worker-line-oversize
    "oversize": '    sys.stdout.buffer.write(b"x" * 100000 + b"\\n")\n',
    # valid frame for another job -> cross-job-output
    "forged": (
        '    import json\n'
        '    frame = {"v": 1, "job_id": "b2-forged-job-X", "seq": 0,\n'
        '               "type": "started", "payload": {}}\n'
        '    sys.stdout.buffer.write(json.dumps(frame).encode() + b"\\n")\n'
    ),
    # valid envelope, unknown event type -> unknown-worker-event
    "unknown_event": (
        '    import json\n'
        '    frame = {"v": 1, "job_id": _job(), "seq": 0,\n'
        '               "type": "pwned", "payload": {}}\n'
        '    sys.stdout.buffer.write(json.dumps(frame).encode() + b"\\n")\n'
    ),
    # valid envelope, wrong sequence -> worker-seq-gap
    "seq_gap": (
        '    import json\n'
        '    frame = {"v": 1, "job_id": _job(), "seq": 9,\n'
        '               "type": "started", "payload": {}}\n'
        '    sys.stdout.buffer.write(json.dumps(frame).encode() + b"\\n")\n'
    ),
}

_BADWORKER_ERRORS = {
    "garbage": "malformed-worker-output",
    "oversize": "oversize-worker-line",
    "forged": "cross-job-output",
    "unknown_event": "unknown-worker-event",
    "seq_gap": "worker-seq-gap",
}


@contextlib.contextmanager
def _worker_pythonpath(extra):
    """Set an ABSOLUTE worker PYTHONPATH (worker cwd differs from ours, so
    the suite's relative ``PYTHONPATH=src`` would not resolve there)."""
    old = os.environ.get("PYTHONPATH")
    parts = [str(item) for item in extra]
    if old:
        parts.extend(piece for piece in old.split(os.pathsep)
                     if piece and os.path.isabs(piece))
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old


@contextlib.contextmanager
def _worker_module_override(name):
    env_key = "GHOSTLAB_BACKEND_WORKER_MODULE"
    old = os.environ.get(env_key)
    if name is None:
        os.environ.pop(env_key, None)
    else:
        os.environ[env_key] = name
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = name


class _Listener:
    """Harness-owned 127.0.0.1 TCP listener recording EXTERNAL hits."""

    def __init__(self):
        self.hits = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(2.0)
                try:
                    data = conn.recv(4096)
                except OSError:
                    data = b""
                self.hits.append(data)

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)


class _AdversarialCases:
    """Backend-parameterized adversarial checks (mixin; see concrete below).

    Every check ends in :meth:`_verdict` against ``EXPECTED``: enforced
    means the backend demonstrably contained the attack; absent means the
    attack observably SUCCEEDED (proving the check detects).
    """

    BACKEND_NAME = None
    BACKEND_FACTORY = None

    @classmethod
    def setUpClass(cls):
        if not (SRCS / "ghostlab").is_dir():  # pragma: no cover
            raise AssertionError(f"repo layout changed: {SRCS}/ghostlab missing")
        holder = tempfile.TemporaryDirectory(prefix="b2-shim-")
        cls.addClassCleanup(holder.cleanup)
        shim = Path(holder.name)
        (shim / "ghostlab").mkdir()
        (shim / "ghostlab" / "__init__.py").write_text(_SHIM_INIT)
        (shim / "ghostlab" / "b2_probe.py").write_text(_PROBE_SOURCE)
        (shim / "ghostlab" / "b2_booking.py").write_text(_BOOKING_SHIM)
        for mode, body in _BADWORKER_BODIES.items():
            (shim / f"b2bad_{mode}.py").write_text(
                _BADWORKER_TEMPLATE.replace("{mode}", mode).replace("{BODY}", body))
        # Loud early failure if any embedded instrument has a syntax error.
        for script in list((shim / "ghostlab").glob("*.py")) + list(shim.glob("b2bad_*.py")):
            py_compile.compile(str(script), doraise=True)
        cls.shim_dir = str(shim)

    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="b2-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.backend = self.BACKEND_FACTORY(str(self.tmp / "wsroot"))

    # -- harness ------------------------------------------------------

    def _tag(self, check):
        return f"[{self.BACKEND_NAME}/{check}]"

    def _verdict(self, check, contained, *, escaped_detail, contained_detail,
                 missing_boundary):
        """Honesty gate: assert the profiled outcome for one check."""
        tag = self._tag(check)
        expectation = EXPECTED[self.BACKEND_NAME][check]
        if expectation == ENFORCED:
            self.assertTrue(
                contained,
                f"{tag} profiled ENFORCED but the attack escaped: {escaped_detail}")
        elif expectation == ABSENT:
            self.assertFalse(
                contained,
                f"{tag} profiled ABSENT but the attack was contained "
                f"(vacuous check, proves nothing): {contained_detail}; "
                f"missing boundary: {missing_boundary}")
        else:  # pragma: no cover
            raise AssertionError(f"{tag} has no profile")

    def _exchange(self, job_id, command, expect, timeout_s=60):
        """Send one worker command, return the first event of type ``expect``.

        Any terminal failure event first is a loud error carrying the event.
        """
        self.backend.send(job_id, command)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for event in self.backend.poll(job_id, timeout_ms=1000):
                if event["type"] == expect:
                    return event
                if event["type"] in ("failed", "truncated"):
                    raise AssertionError(
                        f"{self._tag('harness')} job died waiting for "
                        f"{expect}: {json.dumps(event)[:500]}")
            if self.backend.status(job_id)["state"] in TERMINAL_STATES:
                raise AssertionError(
                    f"{self._tag('harness')} job terminal before {expect}")
        raise AssertionError(
            f"{self._tag('harness')} timed out waiting for {expect}")

    def _attack(self, job_id, name, args=None, timeout_s=60):
        """Run one probe attack; return its result (asserts the round-trip)."""
        payload = {"tool": "attack",
                   "arguments": {"name": name, **(args or {})}}
        event = self._exchange(job_id, {"type": "invoke", "payload": payload},
                               "tool_result", timeout_s)
        body = event["payload"]
        self.assertTrue(body.get("ok"),
                        f"attack {name} failed inside job: {body}")
        return body.get("result", {})

    @contextlib.contextmanager
    def _job(self, *, adapter_ref=PROBE_REF, fixture=None, limits=None,
             project_id="b2-probe", job_id=None, extra_path=(),
             worker_module=None, hello=True):
        """Start a checked-in job (worker env juggling covers start only)."""
        tag = job_id or f"b2-{secrets.token_hex(4)}"
        spec_limits = dict(limits or {"wall_clock_ms": 120000,
                                      "max_output_bytes": 1048576})
        with _worker_pythonpath(extra_path), _worker_module_override(worker_module):
            package = {"kind": "checked-in", "identity": adapter_ref}
            self.backend.prepare(dict(package))
            spec = JobSpec(job_id=tag, project_id=project_id,
                           package=package, adapter_ref=adapter_ref,
                           fixture=dict(fixture or {}), limits=spec_limits,
                           no_network=True)
            self.backend.start_job(spec)
        try:
            if hello:
                started = self._exchange(
                    tag, {"type": "hello", "payload": {}}, "started")
                self.assertEqual(started["payload"].get("adapter"), adapter_ref)
            yield tag
        finally:
            try:
                self.backend.close(tag)
            except BackendError:
                pass

    def _probe_job(self, **kwargs):
        kwargs.setdefault("extra_path", (self.shim_dir, str(SRCS)))
        return self._job(**kwargs)

    # -- capabilities honesty ------------------------------------------

    def test_capabilities_honesty(self):
        caps = self.backend.capabilities()
        self.assertEqual(self.backend.version, BACKEND_VERSION)
        self.assertEqual(self.backend.name, self.BACKEND_NAME)
        self.assertEqual(set(caps), {"isolation", "limits", "qualification"})
        self.assertEqual(set(caps["isolation"]),
                         {"filesystem", "process", "network"})
        # The reference backend must NEVER claim isolation it lacks.
        self.assertEqual(caps["qualification"].get("status"), "unqualified",
                         "local reference backend must stay unqualified")

    # -- filesystem boundary (absent for local) --------------------------

    def test_host_canary_read(self):
        token = secrets.token_hex(16)
        canary = self.tmp / "host-canary.txt"
        canary.write_text(token)
        with self._probe_job() as job:
            result = self._attack(job, "read_file", {"path": str(canary)})
        escaped = result.get("content") == token
        self._verdict("host_canary_read", not escaped,
                      escaped_detail="host canary was NOT readable",
                      contained_detail=f"host canary read returned {result!r}",
                      missing_boundary="no filesystem boundary: job shares "
                      "the host filesystem (capabilities filesystem=none)")

    def test_sibling_canary_read(self):
        token = secrets.token_hex(16)
        with self._probe_job(project_id="b2-sib-a", job_id="b2-siba") as job_a:
            home_a = self._attack(job_a, "cwd")["cwd"]
            canary = os.path.join(home_a, "sibling-canary.txt")
            self._attack(job_a, "write_file",
                         {"path": canary, "content": token})
            with self._probe_job(project_id="b2-sib-b",
                                 job_id="b2-sibb") as job_b:
                result = self._attack(job_b, "read_file", {"path": canary})
        escaped = result.get("content") == token
        self._verdict("sibling_canary_read", not escaped,
                      escaped_detail="sibling workspace was NOT readable",
                      contained_detail=f"sibling read returned {result!r}",
                      missing_boundary="no per-job filesystem boundary: "
                      "sibling workspaces are plain host directories")

    def test_write_outside_workspace(self):
        token = secrets.token_hex(16)
        target = self.tmp / "outside" / "escape.txt"
        with self._probe_job() as job:
            home = self._attack(job, "cwd")["cwd"]
            self.assertFalse(os.path.realpath(str(target)).startswith(
                os.path.realpath(home) + os.sep))
            self._attack(job, "write_file",
                         {"path": str(target), "content": token})
        # EXTERNAL: host-side file assert, never the job's word.
        escaped = target.is_file() and target.read_text() == token
        self._verdict("write_outside_workspace", not escaped,
                      escaped_detail="job could NOT write outside its workspace",
                      contained_detail=f"outside file state: exists={target.is_file()}",
                      missing_boundary="no filesystem boundary: absolute host "
                      "paths are writable from the job")

    def test_traversal_via_outputs(self):
        token = secrets.token_hex(16)
        with self._probe_job() as job:
            home = self._attack(job, "cwd")["cwd"]
            traversal = os.path.join(home, "sub", "..", "..",
                                     f"trav-{token}.txt")
            self.assertIn("..", traversal)
            self._attack(job, "write_file",
                         {"path": traversal, "content": token})
            landed = os.path.realpath(traversal)
            self.assertFalse(landed.startswith(os.path.realpath(home) + os.sep))
        escaped = (os.path.isfile(landed)
                   and Path(landed).read_text() == token)
        self._verdict("traversal_via_outputs", not escaped,
                      escaped_detail="traversal write did NOT escape",
                      contained_detail=f"traversal target state: exists="
                      f"{os.path.isfile(landed)}",
                      missing_boundary="no output-path validation or "
                      "workspace jail: '..' segments resolve to host paths")

    def test_symlink_escape(self):
        token = secrets.token_hex(16)
        target = self.tmp / "outside" / "via-link.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._probe_job() as job:
            home = self._attack(job, "cwd")["cwd"]
            link = os.path.join(home, "b2-link")
            probe = self._attack(job, "symlink_probe",
                                 {"link": link, "target": str(target)})
            if not probe.get("created"):
                self.skipTest(
                    f"platform refused symlink creation (errno "
                    f"{probe.get('errno')}: {probe.get('message')}); "
                    "traversal coverage carries the escape verdict on this host")
            self._attack(job, "write_file",
                         {"path": link, "content": token})
        escaped = target.is_file() and target.read_text() == token
        self._verdict("symlink_escape", not escaped,
                      escaped_detail="symlink write did NOT escape",
                      contained_detail=f"link target state: exists={target.is_file()}",
                      missing_boundary="no symlink policy: links resolve to "
                      "arbitrary host paths")

    # -- network boundary (absent for local) -----------------------------

    def test_metadata_endpoint(self):
        with self._probe_job() as job:
            start = time.monotonic()
            result = self._attack(job, "http_metadata",
                                  {"host": "169.254.169.254", "port": 80,
                                   "timeout_s": 2}, timeout_s=30)
            elapsed = time.monotonic() - start
        # Observable success = the job performed unmediated socket syscalls
        # (literal credential exfil is impossible off-cloud; the hard egress
        # proof is test_listener_egress). Any genuine OS-level outcome
        # counts; a backend policy denial would fail the round-trip instead.
        escaped = (result.get("outcome") in
                   ("connected", "socket-error", "io-error")
                   and elapsed < 15)
        self._verdict("metadata_endpoint", not escaped,
                      escaped_detail="metadata attempt was policy-denied",
                      contained_detail=f"metadata probe returned {result!r}",
                      missing_boundary="no network boundary: no_network=True "
                      "is unenforced (capabilities network=host)")

    def test_listener_egress(self):
        token = secrets.token_hex(16)
        listener = _Listener()
        try:
            with self._probe_job() as job:
                result = self._attack(
                    job, "tcp_connect",
                    {"host": "127.0.0.1", "port": listener.port,
                     "token": token, "timeout_s": 5})
            deadline = time.monotonic() + 5
            while not listener.hits and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            listener.close()
        # EXTERNAL: harness-owned listener; isolated backends must show ZERO.
        hits = [hit for hit in listener.hits if token.encode() in hit]
        escaped = bool(hits) and result.get("connected") is True
        self._verdict("listener_egress", not escaped,
                      escaped_detail=f"zero listener hits ({len(listener.hits)})",
                      contained_detail=f"listener hits: {len(listener.hits)}",
                      missing_boundary="no network boundary: job reaches "
                      "harness sockets (capabilities network=host)")

    # -- resource boundary (absent for local) ----------------------------

    def test_fork_burst(self):
        count, token = 64, secrets.token_hex(8)
        outdir = self.tmp / "children"
        with self._probe_job(
                limits={"wall_clock_ms": 180000,
                        "max_output_bytes": 1048576}) as job:
            result = self._attack(job, "spawn_many",
                                  {"count": count, "outdir": str(outdir),
                                   "token": token, "timeout_s": 60},
                                  timeout_s=120)
            # Job must still be healthy afterwards (no backend pid cap hit).
            echo = self._attack(job, "echo", {"value": "alive"})
        # EXTERNAL: every child wrote exactly one host-side file and exited.
        files = sorted(outdir.glob("child-*.txt")) if outdir.is_dir() else []
        codes = result.get("codes", [])
        escaped = (len(files) == count and len(codes) == count
                   and all(code == 0 for code in codes)
                   and echo.get("value") == "alive"
                   and (outdir / "child-0.txt").read_text() == f"{token}-0")
        self._verdict("fork_burst", not escaped,
                      escaped_detail="fork burst was curtailed by the backend",
                      contained_detail=f"files={len(files)}/{count} "
                      f"codes={codes!r} echo={echo!r}",
                      missing_boundary="no pids limit: unbounded child "
                      "processes run to completion")

    def test_cpu_spin(self):
        with self._probe_job() as job:
            start = time.monotonic()
            result = self._attack(job, "cpu_spin", {"seconds": 3.0},
                                  timeout_s=60)
            wall = time.monotonic() - start
            echo = self._attack(job, "echo", {"value": "alive"})
        escaped = (result.get("cpu_s", 0) >= 2.0 and wall >= 2.5
                   and echo.get("value") == "alive")
        self._verdict("cpu_spin", not escaped,
                      escaped_detail="CPU burn was throttled or killed",
                      contained_detail=f"cpu={result!r} wall={wall:.1f}s "
                      f"echo={echo!r}",
                      missing_boundary="no CPU quota: 3s of burn runs "
                      "unthrottled and the job stays healthy")

    def test_mem_grow(self):
        size = 256 * 1024 * 1024
        with self._probe_job() as job:
            result = self._attack(job, "mem_grow", {"bytes": size},
                                  timeout_s=90)
            echo = self._attack(job, "echo", {"value": "alive"})
        escaped = (result.get("held") == size
                   and echo.get("value") == "alive")
        self._verdict("mem_grow", not escaped,
                      escaped_detail="256MB growth was refused or OOM-killed",
                      contained_detail=f"mem={result!r} echo={echo!r}",
                      missing_boundary="no memory quota: 256MB growth "
                      "succeeds and the job stays healthy")

    def test_disk_fill(self):
        size = 64 * 1024 * 1024
        with self._probe_job() as job:
            home = self._attack(job, "cwd")["cwd"]
            target = os.path.join(home, "disk-64m.bin")
            result = self._attack(job, "disk_fill",
                                  {"path": target, "bytes": size},
                                  timeout_s=90)
            # EXTERNAL stat before close() reaps the workspace.
            actual = os.path.getsize(target) if os.path.isfile(target) else -1
        escaped = result.get("size") == size and actual == size
        self._verdict("disk_fill", not escaped,
                      escaped_detail="64MB workspace write was quota-denied",
                      contained_detail=f"reported={result!r} host_stat={actual}",
                      missing_boundary="no disk quota: 64MB lands in the "
                      "workspace (host-observed)")

    # -- output protocol (enforced for local) ----------------------------

    def test_oversize_event(self):
        with self._probe_job() as job:
            self.backend.send(job, {"type": "invoke", "payload": {
                "tool": "attack",
                "arguments": {"name": "big_result", "bytes": 1024 * 1024}}})
            events, deadline = [], time.monotonic() + 30
            terminal = None
            while time.monotonic() < deadline and terminal is None:
                for event in self.backend.poll(job, timeout_ms=1000):
                    events.append(event)
                    if event["type"] in ("failed", "truncated"):
                        terminal = event
                if (terminal is None and self.backend.status(job)["state"]
                        in TERMINAL_STATES):
                    for event in self.backend.poll(job, timeout_ms=2000):
                        events.append(event)
                        if event["type"] in ("failed", "truncated"):
                            terminal = event
                    break
            state = self.backend.status(job)["state"]
        # Contained = the 1MB emission never crossed as a valid event: the
        # backend failed the job instead of crashing or relaying it.
        leaked = [event for event in events
                  if len(json.dumps(event.get("payload", {}))) > 65536]
        contained = (terminal is not None and not leaked
                     and state in ("failed", "cancelled", "timed_out"))
        # Positive control: the controller survives; a fresh job still runs.
        with self._probe_job() as job2:
            echo = self._attack(job2, "echo", {"value": "alive"})
        contained = contained and echo.get("value") == "alive"
        self._verdict("oversize_event", contained,
                      escaped_detail=f"1MB output leaked or controller died: "
                      f"state={state} leaked={len(leaked)} events={len(events)}",
                      contained_detail="oversize emission relayed as valid output",
                      missing_boundary="")

    def test_malformed_event(self):
        for mode, want_error in _BADWORKER_ERRORS.items():
            with self.subTest(worker=mode):
                job = f"b2-bad-{mode}-{secrets.token_hex(2)}"
                with _worker_pythonpath((self.shim_dir, str(SRCS))), \
                        _worker_module_override(f"b2bad_{mode}"):
                    package = {"kind": "checked-in", "identity": PROBE_REF}
                    self.backend.prepare(dict(package))
                    self.backend.start_job(JobSpec(
                        job_id=job, project_id="b2-probe", package=package,
                        adapter_ref=PROBE_REF, fixture={},
                        limits={"wall_clock_ms": 30000,
                                "max_output_bytes": 1048576},
                        no_network=True))
                try:
                    terminal, deadline = None, time.monotonic() + 20
                    while time.monotonic() < deadline and terminal is None:
                        for event in self.backend.poll(job, timeout_ms=1000):
                            if event["type"] in ("failed", "truncated"):
                                terminal = event
                        if (terminal is None
                                and self.backend.status(job)["state"]
                                in TERMINAL_STATES):
                            break
                    state = self.backend.status(job)["state"]
                finally:
                    try:
                        self.backend.close(job)
                    except BackendError:
                        pass
                contained = (terminal is not None
                             and want_error in json.dumps(terminal.get("payload", {}))
                             and state == "failed")
                self._verdict("malformed_event", contained,
                              escaped_detail=f"{mode}: no fail-closed "
                              f"(terminal={terminal} state={state})",
                              contained_detail=f"{mode} output accepted",
                              missing_boundary="")
        # Positive control: the controller survives every bad worker.
        with self._probe_job() as job:
            echo = self._attack(job, "echo", {"value": "alive"})
        self.assertEqual(echo.get("value"), "alive")

    # -- cancellation (absent for local: descendants survive) -------------

    def test_cancel_survival(self):
        token = secrets.token_hex(16)
        flag = self.tmp / "detached" / "survived.txt"
        script = self.tmp / "detached" / "sleeper.py"
        script_text = (
            "import time\n"
            "time.sleep(5)\n"
            f"open({str(flag)!r}, 'w').write({token!r})\n")
        with self._probe_job() as job:
            child = self._attack(job, "spawn_detached",
                                 {"script": str(script),
                                  "script_text": script_text})
            self.assertIs(type(child.get("pid")), int)
            outcome = self.backend.cancel(job, grace_ms=2000)
            self.assertEqual(outcome.get("state"), "cancelled")
            self.assertTrue(outcome.get("confirmed"))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not flag.is_file():
                time.sleep(0.25)
        # EXTERNAL: host-side file written by the detached grandchild AFTER
        # cancel confirmed. Present = survived (absent); missing = reaped.
        escaped = flag.is_file() and flag.read_text() == token
        self._verdict("cancel_survival", not escaped,
                      escaped_detail="detached child was reaped on cancel",
                      contained_detail=f"survival flag exists={flag.is_file()}",
                      missing_boundary="no descendant kill: cancel terminates "
                      "the direct child only (no job object / tree kill)")

    # -- identity / controller posture (enforced for local) ---------------

    def test_forged_job_id(self):
        ghost = f"b2-ghost-{secrets.token_hex(4)}"
        with self.assertRaises(BackendError):
            self.backend.send(ghost, {"type": "hello", "payload": {}})
        with self.assertRaises(BackendError):
            self.backend.poll(ghost, timeout_ms=0)
        with self.assertRaises(BackendError):
            self.backend.status(ghost)
        with self.assertRaises(BackendError):
            self.backend.cancel(ghost, grace_ms=0)
        with self.assertRaises(BackendError):
            self.backend.close(ghost)
        with self._probe_job() as job:
            with self.assertRaises(BackendError):
                self.backend.send(job, {"type": "no-such-command"})
            with self.assertRaises(BackendError):
                self.backend.send(job, {"payload": {}})
            with self.assertRaises(BackendError):
                self.backend.send(job, {"type": "invoke", "payload": []})
            with self.assertRaises(BackendError):
                self.backend.poll(job, timeout_ms=-1)
            with self.assertRaises(BackendError):
                self.backend.cancel(job, grace_ms=-1)
            with self.assertRaises(BackendError):
                dupe = JobSpec(job_id=job, project_id="b2-probe",
                               package={"kind": "checked-in",
                                        "identity": PROBE_REF},
                               adapter_ref=PROBE_REF, fixture={},
                               limits={"wall_clock_ms": 1000,
                                       "max_output_bytes": 1024},
                               no_network=True)
                self.backend.start_job(dupe)
        with self.assertRaises(ValueError):
            JobSpec(job_id="../escape", project_id="b2-probe",
                    package={"kind": "checked-in", "identity": PROBE_REF},
                    adapter_ref=PROBE_REF, fixture={},
                    limits={"wall_clock_ms": 1000, "max_output_bytes": 1024},
                    no_network=True)
        # Protocol-level cross-job mapping: a frame for another job is an
        # identity mismatch, never silently accepted.
        forged = _protocol.encode_message("b2-other", 0, "started", {})
        with self.assertRaises(ProtocolError):
            _protocol.decode_message(forged, expected_job=ghost)
        self._verdict("forged_job_id", True,
                      escaped_detail="forged/unknown job id accepted",
                      contained_detail="",
                      missing_boundary="")

    def test_hello_protocol(self):
        # invoke before hello must fail the job closed (worker requires the
        # handshake first; the backend relays the failure, never runs blind).
        with self._probe_job(hello=False) as job:
            self.backend.send(job, {"type": "invoke", "payload": {
                "tool": "attack", "arguments": {"name": "echo"}}})
            terminal, deadline = None, time.monotonic() + 20
            while time.monotonic() < deadline and terminal is None:
                for event in self.backend.poll(job, timeout_ms=1000):
                    if event["type"] == "failed":
                        terminal = event
            state = self.backend.status(job)["state"]
        contained = (terminal is not None and state == "failed"
                     and "hello" in json.dumps(terminal.get("payload", {})))
        # Duplicate hello likewise fails closed.
        with self._probe_job() as job2:
            self.backend.send(job2, {"type": "hello", "payload": {}})
            terminal2, deadline = None, time.monotonic() + 20
            while time.monotonic() < deadline and terminal2 is None:
                for event in self.backend.poll(job2, timeout_ms=1000):
                    if event["type"] == "failed":
                        terminal2 = event
            state2 = self.backend.status(job2)["state"]
        contained = (contained and terminal2 is not None
                     and state2 == "failed")
        self._verdict("hello_protocol", contained,
                      escaped_detail=f"handshake violation ran anyway "
                      f"(states {state}/{state2})",
                      contained_detail="",
                      missing_boundary="")

    def test_env_scrub(self):
        marker = f"GHOSTLAB_B2_SECRET_{secrets.token_hex(8)}"
        value = secrets.token_hex(16)
        os.environ[marker] = value
        try:
            with self._probe_job() as job:
                result = self._attack(job, "env_dump")
                env = result.get("env", {})
        finally:
            os.environ.pop(marker, None)
        # Positive control: the echo channel is live (backend-set vars land).
        self.assertIn("GHOSTLAB_JOB_ID", env)
        contained = marker not in env and value not in " ".join(env.values())
        self._verdict("env_scrub", contained,
                      escaped_detail=f"controller secret leaked to worker env",
                      contained_detail="",
                      missing_boundary="")

    def test_no_host_path_write(self):
        token = secrets.token_hex(12)
        decoy_abs = str(self.tmp / f"decoy-{token}.txt")
        decoy_trav = os.path.join("..", "..", f"decoy-trav-{token}.txt")
        with self._probe_job(fixture={decoy_abs: "untrusted",
                                      decoy_trav: "untrusted"}) as job:
            self.backend.send(job, {"type": "reset", "payload": {
                "fixture": {decoy_abs: "untrusted"}}})
            event = self._exchange(job, {"type": "evidence", "payload": {}},
                                   "evidence_chunk")
            self.assertEqual(event["payload"]["evidence"]["probe"], "b2")
            self._attack(job, "echo", {"value": decoy_abs})
        # EXTERNAL: untrusted path strings crossed the channel; the
        # controller must never materialize them as host files.
        escaped = (os.path.exists(decoy_abs)
                   or os.path.exists(os.path.join(str(ROOT), decoy_trav)))
        self._verdict("no_host_path_write", not escaped,
                      escaped_detail="controller wrote a job-supplied host path",
                      contained_detail="",
                      missing_boundary="")

    # -- usability: booking round-trip with correct verdict inputs --------

    def test_usability_booking_roundtrip(self):
        from examples.equipment_booking.env_adapter import BookingEnvAdapter
        identity = {"equipment_id": BOOKING_FIXTURE["equipment_id"],
                    "user_id": BOOKING_FIXTURE["user_id"]}
        direct = BookingEnvAdapter()
        direct.reset(dict(BOOKING_FIXTURE))
        exp_get = direct.invoke("get_equipment", dict(identity))
        exp_book = direct.invoke("book_equipment", {
            **identity, "slot": BOOKING_FIXTURE["slot"],
            "idempotency_key": "b2-use-1"})
        exp_snapshot = direct.snapshot()
        exp_evidence = direct.evidence()

        with self._job(adapter_ref=BOOKING_REF,
                       extra_path=(self.shim_dir, str(ROOT), str(SRCS))) as job:
            reset_ev = self._exchange(job, {"type": "reset", "payload": {
                "fixture": dict(BOOKING_FIXTURE)}}, "tool_result")
            self.assertTrue(reset_ev["payload"].get("ok"), reset_ev)
            got = self._exchange(job, {"type": "invoke", "payload": {
                "tool": "get_equipment", "arguments": dict(identity)}},
                "tool_result")["payload"]
            self.assertTrue(got.get("ok"), got)
            self.assertEqual(got.get("result"), exp_get)
            booked = self._exchange(job, {"type": "invoke", "payload": {
                "tool": "book_equipment", "arguments": {
                    **identity, "slot": BOOKING_FIXTURE["slot"],
                    "idempotency_key": "b2-use-1"}}},
                "tool_result")["payload"]
            self.assertTrue(booked.get("ok"), booked)
            self.assertEqual(booked.get("result"), exp_book)
            snap = self._exchange(job, {"type": "snapshot", "payload": {}},
                                  "tool_result")["payload"]
            self.assertTrue(snap.get("ok"), snap)
            self.assertEqual(snap.get("result"), exp_snapshot)
            chunk = self._exchange(job, {"type": "evidence", "payload": {}},
                                   "evidence_chunk")["payload"]
            self.assertEqual(chunk.get("evidence"), exp_evidence)
            fault = self._exchange(job, {"type": "set_fault", "payload": {
                "name": "timeout_after_commit", "params": {}}},
                "tool_result")["payload"]
            self.assertTrue(fault.get("ok"), fault)
            flaky = self._exchange(job, {"type": "invoke", "payload": {
                "tool": "book_equipment", "arguments": {
                    **identity, "slot": BOOKING_FIXTURE["slot"],
                    "idempotency_key": "b2-use-2"}}},
                "tool_result")["payload"]
            self.assertFalse(flaky.get("ok"), flaky)
            self.assertEqual(flaky.get("error"), "BookingTimeout")
            clear = self._exchange(job, {"type": "clear_faults", "payload": {}},
                                   "tool_result")["payload"]
            self.assertTrue(clear.get("ok"), clear)
            done = self._exchange(job, {"type": "close", "payload": {}},
                                  "completed")
            self.assertEqual(done["type"], "completed")
            self.assertEqual(self.backend.status(job)["state"], "completed")


class ProtocolStrictnessTests(unittest.TestCase):
    """Frozen protocol constants and strict-codec enforcement (no backend)."""

    def test_frozen_constants(self):
        self.assertEqual(_protocol.PROTOCOL_VERSION, 1)
        self.assertEqual(_protocol.MAX_MESSAGE_BYTES, 64 * 1024)
        self.assertEqual(_protocol.MAX_DEPTH, 8)
        self.assertEqual(_protocol.WORKER_COMMANDS,
                         {"hello", "reset", "invoke", "set_fault",
                          "clear_faults", "evidence", "snapshot", "close"})
        self.assertEqual(_protocol.WORKER_EVENTS,
                         {"started", "tool_result", "evidence_chunk",
                          "completed", "failed", "truncated"})

    def test_no_pickle_or_eval_on_protocol_path(self):
        source = Path(_protocol.__file__).read_text(encoding="utf-8")
        for marker in ("import pickle", "from pickle", "pickle.", "eval(",
                       "exec(", "__import__"):
            self.assertNotIn(marker, source)

    def test_roundtrip(self):
        raw = _protocol.encode_message("b2-job-1", 3, "invoke",
                                       {"tool": "t", "n": 1,
                                        "flag": True, "nothing": None,
                                        "list": [1, "a"]})
        message = _protocol.decode_message(
            raw, expected_job="b2-job-1", expected_seq=3)
        self.assertEqual(message, {"v": 1, "job_id": "b2-job-1", "seq": 3,
                                  "type": "invoke",
                                  "payload": {"tool": "t", "n": 1,
                                              "flag": True, "nothing": None,
                                              "list": [1, "a"]}})

    def test_size_cap(self):
        with self.assertRaises(ProtocolError):
            _protocol.encode_message("j", 0, "t", {"blob": "x" * (1024 * 1024)})
        with self.assertRaises(ProtocolError):
            _protocol.decode_message(b"x" * (1024 * 1024))
        # Exact 64KB boundary: a valid 65536-byte line decodes, 65537 dies.
        prefix = b'{"v":1,"job_id":"j","seq":0,"type":"t","payload":{"s":"'
        suffix = b'"}}'
        line_ok = prefix + b"x" * (65536 - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(line_ok), 65536)
        self.assertEqual(_protocol.decode_message(line_ok)["job_id"], "j")
        with self.assertRaises(ProtocolError):
            _protocol.decode_message(line_ok + b"x")

    def test_depth_cap(self):
        payload, deep = {}, {}
        for _ in range(8):
            deep = {"a": deep} if deep else {"a": 1}
        payload = deep
        _protocol.encode_message("j", 0, "t", payload)  # depth 8: allowed
        too_deep = {"a": payload}  # depth 9
        with self.assertRaises(ProtocolError):
            _protocol.encode_message("j", 0, "t", too_deep)
        raw = json.dumps({"v": 1, "job_id": "j", "seq": 0, "type": "t",
                          "payload": too_deep}).encode()
        with self.assertRaises(ProtocolError):
            _protocol.decode_message(raw)

    def test_strict_types(self):
        bad = [
            b"not json{{{", b"[1,2]", b'"str"', b"42",
            b'{"v":1,"job_id":"j","seq":0,"type":"t"}',  # missing payload
            b'{"v":1,"job_id":"j","seq":0,"type":"t","payload":{},"x":1}',
            b'{"v":2,"job_id":"j","seq":0,"type":"t","payload":{}}',
            b'{"v":"1","job_id":"j","seq":0,"type":"t","payload":{}}',
            b'{"v":1,"job_id":"","seq":0,"type":"t","payload":{}}',
            b'{"v":1,"job_id":"j","seq":-1,"type":"t","payload":{}}',
            b'{"v":1,"job_id":"j","seq":true,"type":"t","payload":{}}',
            b'{"v":1,"job_id":"j","seq":0,"type":"","payload":{}}',
            b'{"v":1,"job_id":"j","seq":0,"type":"t","payload":[]}',
            b'{"v":1,"job_id":"j","seq":0,"type":"t","payload":{"n":NaN}}',
            b'{"v":1,"job_id":"j","seq":0,"type":"t","payload":{"n":Infinity}}',
            b"\xff\xfe invalid utf-8",
        ]
        for raw in bad:
            with self.subTest(raw=raw[:40]), self.assertRaises(ProtocolError):
                _protocol.decode_message(raw)
        with self.assertRaises(ProtocolError):
            _protocol.decode_message(42)
        with self.assertRaises(ProtocolError):
            _protocol.encode_message("j", 0, "t", {"s": {"b"}})
        with self.assertRaises(ProtocolError):
            _protocol.encode_message("j", 0, "t", {"f": float("nan")})
        with self.assertRaises(ProtocolError):
            _protocol.encode_message("", 0, "t", {})
        with self.assertRaises(ProtocolError):
            _protocol.encode_message("j", -1, "t", {})
        with self.assertRaises(ProtocolError):
            _protocol.encode_message("j", True, "t", {})

    def test_identity_mismatch_mapping(self):
        raw = _protocol.encode_message("job-a", 0, "started", {})
        with self.assertRaises(ProtocolError):
            _protocol.decode_message(raw, expected_job="job-b")
        with self.assertRaises(ProtocolError):
            _protocol.decode_message(raw, expected_seq=1)


class LocalBackendAdversarialB2Tests(_AdversarialCases, unittest.TestCase):
    BACKEND_NAME = "local"
    BACKEND_FACTORY = staticmethod(
        lambda root: BACKENDS["local"](root))


if __name__ == "__main__":
    unittest.main()

