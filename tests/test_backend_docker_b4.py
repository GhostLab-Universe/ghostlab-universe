"""TRACK B4: Docker container backend (host-gated) tests.

Reads, in order:

- Host probe tests record the docker client/daemon versions and WSL docker
  presence. They never fail for a missing daemon; they RECORD it.
- Pure unit tests (names, flag assembly, workspace verification, intake
  wiring, Dockerfile statics) run everywhere and must pass.
- Stub-daemon lifecycle tests drive the REAL worker through a fake ``docker``
  executable: they prove protocol/lifecycle handling identical to the local
  reference, and prove NOTHING about isolation.
- Daemon-enforced tests (adversarial profile, usability run, digest pinning)
  run ONLY with a reachable daemon; with no daemon they skip with the
  explicit reason ``no-daemon``. A skip here is REPORTED AS BLOCKED, never
  as passed.

Isolation discipline: every container/image this file creates carries the
``ghostlab-b4-`` prefix plus a unique suffix; teardown list-filters that
prefix and removes everything it created. Other sessions' containers,
images, and processes are never touched.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
import uuid

from ghostlab.backends import docker as b4
from ghostlab.backends import intake as bintake
from ghostlab.backends.interface import (BackendError, BackendUnavailableError,
                                         JobSpec)

REPO_ROOT = __file__
for _ in range(2):
    REPO_ROOT = os.path.dirname(REPO_ROOT)
SRC_ROOT = os.path.join(REPO_ROOT, "src")
DOCKERFILE = os.path.join(REPO_ROOT, "src", "ghostlab", "backends",
                          "Dockerfile.worker")
BASE_IMAGE = "python:3.13-slim"
REFUND_ADAPTER = "ghostlab.envs.adapters:RefundEnvAdapter"


# -- shared helpers -----------------------------------------------------

def make_spec(job_id, *, package=None, adapter_ref=REFUND_ADAPTER,
              no_network=True, wall_clock_ms=30000, max_output_bytes=65536):
    return JobSpec(
        job_id=job_id, project_id="b4-tests",
        package=dict(package) if package is not None else {
            "kind": "checked-in", "identity": "b4-test"},
        adapter_ref=adapter_ref, fixture={},
        limits={"wall_clock_ms": wall_clock_ms,
                "max_output_bytes": max_output_bytes},
        no_network=no_network)


def make_tar_gz(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


_DAEMON_PROBE = None


def daemon_probe():
    global _DAEMON_PROBE
    if _DAEMON_PROBE is None:
        _DAEMON_PROBE = b4.probe_daemon()
    return _DAEMON_PROBE


def require_daemon(testcase):
    probe = daemon_probe()
    if not probe.available:
        testcase.skipTest(f"no-daemon: {probe.error}")
    return probe


_WSL_PROBE = None


def wsl_probe():
    global _WSL_PROBE
    if _WSL_PROBE is None:
        _WSL_PROBE = b4.probe_wsl_docker(timeout_s=45)
    return _WSL_PROBE


# -- fake docker executable (stub daemon; lifecycle only, NO isolation) --

_STUB_SOURCE = textwrap.dedent(r'''
    import json
    import os
    import signal
    import subprocess
    import sys

    STATE = os.environ["GHOSTLAB_B4_FAKE_STATE"]
    DOWN = os.environ.get("GHOSTLAB_B4_FAKE_MODE") == "down"
    # Windows has no SIGKILL; SIGTERM terminates via TerminateProcess there.
    _SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

    VALUE_FLAGS = {"--name", "-e", "--mount", "--memory", "--cpus",
                   "--pids-limit", "--workdir", "--user", "--cap-drop",
                   "--security-opt", "--network"}
    ENV_ALLOW = {"GHOSTLAB_JOB_ID", "GHOSTLAB_ADAPTER_REF",
                 "GHOSTLAB_ALLOWED_PREFIXES", "GHOSTLAB_MAX_OUTPUT_BYTES",
                 "PYTHONPATH"}

    def state_path(name):
        return os.path.join(STATE, name + ".json")

    def alive(pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def fail(msg, code=1):
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()
        return code

    def down(msg="Cannot connect to the Docker daemon. "
                 "Is the docker daemon running?"):
        return fail(msg, 1)

    def cmd_version(argv):
        if DOWN:
            sys.stdout.write("Client:\n Version: 99.9-fake\n")
            sys.stdout.flush()
            return fail("failed to connect to the docker API at "
                        "npipe:////./pipe/dockerDesktopLinuxEngine; "
                        "is the docker daemon running?", 1)
        sys.stdout.write("Client:\n Version: 99.9-fake\n\n"
                         "Server:\n Version: 99.9-fake-server\n")
        sys.stdout.flush()
        return 0

    def cmd_info(argv):
        if DOWN:
            return down()
        sys.stdout.write("99.9-fake-server\n")
        sys.stdout.flush()
        return 0

    def cmd_image(argv):
        if DOWN:
            return down()
        if argv[2:3] == ["inspect"] and len(argv) == 4:
            sys.stdout.write('[{"Id":"sha256:fake",'
                             '"RepoDigests":["fake@sha256:%s"]}]\n'
                             % ("f" * 64))
            sys.stdout.flush()
            return 0
        return fail("stub: unsupported image command", 125)

    def cmd_run(args):
        if DOWN:
            return fail("docker: Cannot connect to the Docker daemon. "
                        "Is the docker daemon running?", 125)
        pairs = []
        env = {}
        name = None
        i = 0
        positional = []
        while i < len(args):
            tok = args[i]
            if tok in VALUE_FLAGS:
                if i + 1 >= len(args):
                    return fail("stub: flag without value", 125)
                val = args[i + 1]
                pairs.append((tok, val))
                if tok == "--name":
                    name = val
                elif tok == "-e":
                    key, _, value = val.partition("=")
                    env[key] = value
                i += 2
            elif tok in ("-i", "--read-only"):
                pairs.append((tok, None))
                i += 1
            elif tok.startswith("-"):
                return fail("stub: rejected flag %s" % tok, 125)
            else:
                positional = args[i:]
                break
        have = {(k, v) for k, v in pairs if v is not None}
        for flag, val in (("--network", "none"), ("--user", "job"),
                           ("--cap-drop", "ALL"),
                           ("--security-opt", "no-new-privileges:true")):
            if (flag, val) not in have:
                return fail("stub: missing %s %s" % (flag, val), 125)
        for flag in ("--pids-limit", "--memory", "--cpus"):
            if not any(k == flag for k, _ in pairs):
                return fail("stub: missing %s" % flag, 125)
        if ("-i", None) not in pairs or ("--read-only", None) not in pairs:
            return fail("stub: -i/--read-only required", 125)
        mounts = [v for k, v in pairs if k == "--mount"]
        if len(mounts) != 3:
            return fail("stub: expected exactly 3 --mount entries", 125)
        if not any(m.startswith("type=bind,src=")
                   and m.endswith(",dst=/work/inputs,readonly")
                   for m in mounts):
            return fail("stub: read-only inputs mount missing", 125)
        if not any(m.startswith("type=tmpfs,dst=/work/scratch,")
                   for m in mounts):
            return fail("stub: scratch tmpfs mount missing", 125)
        if not any(m.startswith("type=tmpfs,dst=/tmp,") for m in mounts):
            return fail("stub: /tmp tmpfs mount missing", 125)
        if not name or not name.startswith("ghostlab-b4-"):
            return fail("stub: owned --name missing", 125)
        for key in env:
            if key not in ENV_ALLOW:
                return fail("stub: forbidden env key %s" % key, 125)
        if len(positional) < 2:
            return fail("stub: image + command required", 125)
        command = positional[1:]
        child_env = dict(os.environ)
        child_env.update(env)
        sys.stdout.flush()
        sys.stderr.flush()
        # NOTE: os.execvpe is unusable here on Windows: the CRT joins argv
        # with spaces and the child re-splits it, so any argument containing
        # a space (every python -c program) arrives truncated. subprocess
        # quotes correctly; the stub parent simply forwards the exit code
        # while kill/rm/inspect act on the recorded child pid.
        try:
            child = subprocess.Popen(command, env=child_env)
        except OSError as error:
            return fail("stub: spawn failed: %s" % error, 125)
        record = {"pid": child.pid, "id": "fake-" + name,
                  "image": positional[0], "argv": command}
        with open(state_path(name), "w") as handle:
            json.dump(record, handle)
        try:
            return child.wait()
        except KeyboardInterrupt:
            try:
                child.kill()
            except OSError:
                pass
            return 125

    def cmd_inspect(argv):
        name = argv[-1]
        path = state_path(name)
        if not os.path.exists(path):
            return fail("Error: No such container: %s" % name, 1)
        with open(path) as handle:
            rec = json.load(handle)
        fmt = ""
        if "--format" in argv:
            fmt = argv[argv.index("--format") + 1]
        if "Name" in fmt:
            sys.stdout.write("/" + name + "\n")
        elif "Running" in fmt:
            sys.stdout.write(
                "true\n" if alive(rec["pid"]) else "false\n")
        else:
            sys.stdout.write(rec.get("id", name) + "\n")
        sys.stdout.flush()
        return 0

    def cmd_kill(argv):
        name = argv[-1]
        sig = "TERM"
        for idx, tok in enumerate(argv):
            if tok.startswith("--signal="):
                sig = tok.split("=", 1)[1]
            elif tok in ("-s", "--signal") and idx + 1 < len(argv):
                sig = argv[idx + 1]
        path = state_path(name)
        if not os.path.exists(path):
            return fail("Error: No such container: %s" % name, 1)
        with open(path) as handle:
            pid = json.load(handle)["pid"]
        signum = {"TERM": signal.SIGTERM, "KILL": _SIGKILL,
                  "SIGTERM": signal.SIGTERM,
                  "SIGKILL": _SIGKILL}.get(
                      sig.upper(), signal.SIGTERM)
        try:
            os.kill(pid, signum)
        except OSError:
            pass
        return 0

    def cmd_rm(argv):
        names = [a for a in argv[2:] if not a.startswith("-")]
        for name in names:
            path = state_path(name)
            if os.path.exists(path):
                try:
                    with open(path) as handle:
                        pid = json.load(handle)["pid"]
                except OSError:
                    pid = None
                if pid is not None:
                    try:
                        os.kill(pid, _SIGKILL)
                    except OSError:
                        pass
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return 0

    def cmd_ps(argv):
        for filename in sorted(os.listdir(STATE)):
            if filename.endswith(".json"):
                sys.stdout.write(filename[:-5] + "\n")
        sys.stdout.flush()
        return 0

    def main(argv):
        os.makedirs(STATE, exist_ok=True)
        if len(argv) < 2:
            return fail("usage: docker <cmd>", 125)
        dispatch = {"version": cmd_version, "info": cmd_info,
                    "image": cmd_image, "run": lambda a: cmd_run(a[2:]),
                    "inspect": cmd_inspect, "kill": cmd_kill, "rm": cmd_rm,
                    "ps": cmd_ps}
        handler = dispatch.get(argv[1])
        if handler is None:
            return fail("stub: unsupported command %s" % argv[1], 125)
        return handler(argv)

    if __name__ == "__main__":
        sys.exit(main(sys.argv))
''')

_STUB_PATH = None


def stub_docker_exe():
    global _STUB_PATH
    if _STUB_PATH is None:
        private = os.path.join(tempfile.gettempdir(),
                               f"ghostlab-b4-{os.getpid()}")
        os.makedirs(private, exist_ok=True)
        path = os.path.join(private, "fake-docker.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_STUB_SOURCE)
        _STUB_PATH = path
    return [sys.executable, _STUB_PATH]


class StubBackendMixin:
    """Per-test stub daemon + workspace (track-private temp state)."""

    def setUp(self):
        self._saved_env = dict(os.environ)
        self.workdir = tempfile.mkdtemp(prefix="ghostlab-b4-test-")
        self.state_dir = os.path.join(self.workdir, "fake-state")
        os.makedirs(self.state_dir, exist_ok=True)
        os.environ["GHOSTLAB_B4_FAKE_STATE"] = self.state_dir
        os.environ.pop("GHOSTLAB_B4_FAKE_MODE", None)
        self.root = os.path.join(self.workdir, "workspaces")
        os.makedirs(self.root, exist_ok=True)
        self.jobs: list[str] = []
        super().setUp()

    def tearDown(self):
        try:
            backend = getattr(self, "backend", None)
            for job_id in list(self.jobs):
                try:
                    if backend is not None:
                        backend.close(job_id)
                except BackendError:
                    pass
            if backend is not None:
                try:
                    backend.sweep_own_containers()
                except BackendError:
                    pass
        finally:
            os.environ.clear()
            os.environ.update(self._saved_env)
            shutil.rmtree(self.workdir, ignore_errors=True)
            super().tearDown()

    def make_backend(self, **overrides):
        params = {"docker_exe": stub_docker_exe(), "pythonpath": SRC_ROOT}
        params.update(overrides)
        self.backend = b4.DockerBackend(self.root, **params)
        return self.backend

    def start(self, job_id, **spec_over):
        spec = make_spec(job_id, **spec_over)
        self.backend.start_job(spec)
        self.jobs.append(job_id)
        return spec

    def poll_types(self, job_id, timeout_ms=10000):
        return [event["type"]
                for event in self.backend.poll(job_id, timeout_ms)]


# -- host probe tests (record, never fail for a missing daemon) -----------

class HostProbeTests(unittest.TestCase):
    def test_docker_client_and_daemon_probe(self):
        probe = daemon_probe()
        print(f"\n[B4] docker exe: {probe.docker_exe}")
        print(f"[B4] client version: {probe.client_version}")
        print(f"[B4] server version: {probe.server_version}")
        print(f"[B4] daemon available: {probe.available}")
        print(f"[B4] probe error: {probe.error}")
        self.assertIsInstance(probe.available, bool)
        if probe.available:
            self.assertTrue(probe.docker_exe)
        else:
            self.assertTrue(probe.error,
                            "probe must record why the daemon is unreachable")

    def test_probe_handles_missing_cli(self):
        probe = b4.probe_daemon(
            docker_exe="ghostlab-b4-no-such-exe-xyz")
        self.assertFalse(probe.available)
        self.assertTrue(probe.error)

    def test_wsl_docker_presence_recorded(self):
        record = wsl_probe()
        print(f"\n[B4] wsl present: {record['wsl_present']}")
        print(f"[B4] wsl distros: {record['distros'][:400]}")
        print(f"[B4] docker in wsl: {record['docker_present']}")
        print(f"[B4] wsl docker output: {record['docker_output'][:400]}")
        print(f"[B4] wsl probe error: {record['error']}")
        self.assertIn("wsl_present", record)
        self.assertIn("docker_present", record)

    def test_activation_steps_documented(self):
        steps = b4.activation_steps()
        print(f"\n[B4] activation steps:\n{steps}")
        for marker in ("docker info", "Dockerfile.worker",
                       "{{.Id}}", "test_backend_docker_b4"):
            self.assertIn(marker, steps)


# -- pure unit tests (run everywhere) --------------------------------------

class NamingTests(unittest.TestCase):
    def test_valid_names_carry_prefix_and_unique_suffix(self):
        first = b4.container_name_for_job("job-1")
        second = b4.container_name_for_job("job-1")
        self.assertTrue(first.startswith("ghostlab-b4-job-1-"))
        self.assertNotEqual(first, second)
        self.assertTrue(b4.owns_container_name(first))
        explicit = b4.container_name_for_job("job-1", suffix="abc123")
        self.assertEqual(explicit, "ghostlab-b4-job-1-abc123")

    def test_invalid_job_ids_rejected(self):
        for bad in ("", "has space", "slash/x", "..", "-lead",
                    "semi;colon", "x" * 129, "null\x00", 7, None):
            with self.subTest(job_id=bad):
                with self.assertRaises(BackendError):
                    b4.container_name_for_job(bad)

    def test_foreign_names_not_owned(self):
        self.assertFalse(b4.owns_container_name("other-job-1"))
        self.assertFalse(b4.owns_container_name("ghostlab-b4-"))
        self.assertFalse(b4.owns_container_name("ghostlab-b4-x;y"))
        self.assertFalse(b4.owns_container_name(""))
        self.assertFalse(b4.owns_container_name(None))
        self.assertFalse(b4.owns_container_name(123))


class FlagAssemblyTests(unittest.TestCase):
    def _good(self, **over):
        params = {"docker_exe": "docker",
                  "container_name": "ghostlab-b4-job-1-abc123",
                  "image": "ghostlab-b4-worker",
                  "inputs_dir": os.path.join("tmp", "inputs"),
                  "env": {"GHOSTLAB_JOB_ID": "job-1",
                          "GHOSTLAB_ADAPTER_REF": "m:C",
                          "GHOSTLAB_ALLOWED_PREFIXES": "ghostlab.",
                          "GHOSTLAB_MAX_OUTPUT_BYTES": "1024",
                          "PYTHONPATH": "/work/inputs:/src"},
                  "command": ["python", "-m", "ghostlab.backends.worker"]}
        params.update(over)
        return b4.build_run_args(**params)

    def test_hard_flags_present(self):
        argv = self._good()
        self.assertEqual(argv[:3], ["docker", "run", "-i"])
        for flag, value in (("--name", "ghostlab-b4-job-1-abc123"),
                            ("--network", "none"), ("--user", "job"),
                            ("--cap-drop", "ALL"),
                            ("--security-opt", "no-new-privileges:true")):
            self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--read-only", argv)
        for flag in ("--pids-limit", "--memory", "--cpus"):
            self.assertIn(flag, argv)
        mounts = [argv[i + 1] for i, tok in enumerate(argv)
                  if tok == "--mount"]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(any(m.endswith(",dst=/work/inputs,readonly")
                            for m in mounts))
        self.assertTrue(any(m.startswith("type=tmpfs,dst=/work/scratch,")
                            for m in mounts))
        self.assertTrue(any(m.startswith("type=tmpfs,dst=/tmp,")
                            for m in mounts))
        self.assertEqual(argv[-4:], ["ghostlab-b4-worker", "python", "-m",
                                    "ghostlab.backends.worker"])
        b4.check_hardened_argv(argv)

    def test_audit_rejects_weakening(self):
        base = self._good()
        cases = []
        no_ro = [t for t in base if t != "--read-only"]
        cases.append(no_ro)
        host_net = list(base)
        host_net[host_net.index("none")] = "host"
        cases.append(host_net)
        root_user = list(base)
        root_user[root_user.index("job")] = "root"
        cases.append(root_user)
        cases.append(base + ["--privileged"])
        cases.append(base + ["--device", "/dev/null"])
        cases.append(base + ["--ipc", "host"])
        cases.append(base + ["--pid", "host"])
        cases.append(base + ["-v", "/:/host"])
        other_name = list(base)
        other_name[other_name.index("ghostlab-b4-job-1-abc123")] = "evil-1"
        cases.append(other_name)
        for argv in cases:
            with self.subTest(argv=argv[-3:]):
                with self.assertRaises(BackendError):
                    b4.check_hardened_argv(argv)

    def test_rejects_foreign_env_and_command(self):
        with self.assertRaises(BackendError):
            self._good(env={"MODEL_API_KEY": "secret"})
        with self.assertRaises(BackendError):
            self._good(env={"GHOSTLAB_JOB_ID": "bad\nvalue"})
        with self.assertRaises(BackendError):
            self._good(command=[])
        with self.assertRaises(BackendError):
            self._good(image="image; rm -rf /")
        with self.assertRaises(BackendError):
            self._good(container_name="foreign-1")

    def test_size_and_cpu_validation(self):
        argv = self._good(memory="512m", cpus="2", pids_limit=32,
                         scratch_size=67108864)
        self.assertIn("512m", argv)
        for bad in ("0m", "10x", "", "-5m", 0, None):
            with self.subTest(memory=bad), self.assertRaises(BackendError):
                self._good(memory=bad)
        for bad in ("0", "99", "nan", "", None):
            with self.subTest(cpus=bad), self.assertRaises(BackendError):
                self._good(cpus=bad)
        for bad in (0, -1, True, "64"):
            with self.subTest(pids=bad), self.assertRaises(BackendError):
                self._good(pids_limit=bad)

    def test_bind_src(self):
        if os.name == "nt":
            self.assertEqual(b4.bind_src("C:\\a\\b"), "C:/a/b")
        else:
            self.assertEqual(b4.bind_src("/a/b"), "/a/b")
        with self.assertRaises(BackendError):
            b4.bind_src("")


class WorkspaceVerificationTests(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="ghostlab-b4-verify-")
        super().setUp()

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)
        super().tearDown()

    def test_inside_paths_accepted(self):
        inner = os.path.join(self.workdir, "job-1", "inputs")
        os.makedirs(inner)
        self.assertEqual(
            b4.verify_mount_source(inner, workspace_root=self.workdir),
            os.path.realpath(inner))

    def test_escape_and_outside_rejected(self):
        with self.assertRaises(BackendError):
            b4.verify_mount_source(os.path.join(self.workdir, "..", "evil"),
                                   workspace_root=self.workdir)
        outside = tempfile.mkdtemp(prefix="ghostlab-b4-outside-")
        try:
            with self.assertRaises(BackendError):
                b4.verify_mount_source(outside, workspace_root=self.workdir)
        finally:
            shutil.rmtree(outside, ignore_errors=True)
        with self.assertRaises(BackendError):
            b4.verify_mount_source(os.path.join(self.workdir, "missing"),
                                   workspace_root=self.workdir)

    def test_symlink_escape_rejected(self):
        outside = tempfile.mkdtemp(prefix="ghostlab-b4-outside-")
        try:
            link = os.path.join(self.workdir, "link")
            try:
                os.symlink(outside, link)
            except OSError:
                self.skipTest("symlinks unavailable on this host")
            with self.assertRaises(BackendError):
                b4.verify_mount_source(link, workspace_root=self.workdir)
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class IntakeWiringTests(unittest.TestCase):
    def test_uses_b1_intake_module(self):
        self.assertIs(b4._intake, bintake)

    def test_prepare_accepts_clean_archives(self):
        backend = b4.DockerBackend(tempfile.mkdtemp(prefix="ghostlab-b4-"))
        try:
            data = make_tar_gz({"ghostlab/x.py": b"value = 1\n"})
            result = backend.prepare({"kind": "tar.gz", "data": data})
            self.assertEqual(len(result["identity"]), 64)
            self.assertEqual(result["provenance"]["kind"], "tar.gz")
            checked = backend.prepare({"kind": "checked-in",
                                       "identity": "pin-1"})
            self.assertEqual(checked["identity"], "pin-1")
        finally:
            shutil.rmtree(backend._root, ignore_errors=True)

    def test_prepare_rejects_hostile_archives(self):
        backend = b4.DockerBackend(tempfile.mkdtemp(prefix="ghostlab-b4-"))
        try:
            traversal = make_tar_gz({"../../evil.py": b"evil = 1\n"})
            with self.assertRaises(BackendError):
                backend.prepare({"kind": "tar.gz", "data": traversal})
            with self.assertRaises(BackendError):
                backend.prepare({"kind": "tar.gz", "data": b"not-an-archive"})
            with self.assertRaises(BackendError):
                backend.prepare({"kind": "tar.gz"})
            with self.assertRaises(BackendError):
                backend.prepare({"kind": "squashfs", "data": b"x"})
            with self.assertRaises(BackendError):
                backend.prepare({"no-kind": True})
        finally:
            shutil.rmtree(backend._root, ignore_errors=True)

    def test_prepare_never_executes_payload(self):
        root = tempfile.mkdtemp(prefix="ghostlab-b4-")
        backend = b4.DockerBackend(root)
        try:
            marker = os.path.join(root, "pwned-by-prepare.txt")
            payload = ("open(%r, 'w').write('executed')\n" % marker).encode()
            data = make_tar_gz({"ghostlab/evil.py": payload})
            backend.prepare({"kind": "tar.gz", "data": data})
            self.assertFalse(os.path.exists(marker))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class DockerfileStaticTests(unittest.TestCase):
    def test_worker_dockerfile_pinned_and_minimal(self):
        with open(DOCKERFILE, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("FROM python:3.13-slim\n", text)
        self.assertNotIn(":latest", text)
        self.assertIn("USER job", text)
        self.assertIn('ENTRYPOINT ["python", "-m", "ghostlab.backends.worker"]',
                      text)
        for forbidden in ("pip install", "apt-get", "ADD ", "EXPOSE",
                          "MODEL_API_KEY", "BASETEN_API_KEY"):
            self.assertNotIn(forbidden, text)
        copies = [line for line in text.splitlines()
                  if line.startswith("COPY ")]
        self.assertTrue(copies)
        for line in copies:
            self.assertIn("src/ghostlab/", line)


class OfflineBackendTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ghostlab-b4-")
        self.backend = b4.DockerBackend(self.root)
        super().setUp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def test_capabilities_shape_exact(self):
        caps = self.backend.capabilities()
        self.assertEqual(set(caps), {"isolation", "limits", "qualification"})
        self.assertEqual(set(caps["isolation"]),
                         {"filesystem", "process", "network"})
        self.assertEqual(caps["isolation"]["network"], "none")
        self.assertIsInstance(caps["limits"], list)
        self.assertIn(caps["qualification"]["status"],
                      {"blocked-no-daemon", "unqualified", "qualified",
                       "failed"})
        self.assertLessEqual(set(caps["qualification"]),
                             {"status", "report_id"})

    def test_capabilities_blocked_without_daemon(self):
        if daemon_probe().available:
            self.skipTest("daemon present; blocked-state covered elsewhere")
        caps = self.backend.capabilities()
        self.assertEqual(caps["qualification"]["status"], "blocked-no-daemon")

    def test_unknown_job_operations_refused(self):
        with self.assertRaises(BackendError):
            self.backend.send("nope", {"type": "hello"})
        with self.assertRaises(BackendError):
            self.backend.poll("nope", 0)
        with self.assertRaises(BackendError):
            self.backend.status("nope")
        with self.assertRaises(BackendError):
            self.backend.cancel("nope", 0)
        with self.assertRaises(BackendError):
            self.backend.close("nope")

    def test_start_refuses_networked_jobs_offline(self):
        spec = make_spec("job-net", no_network=False)
        with self.assertRaises(BackendError) as caught:
            self.backend.start_job(spec)
        self.assertIs(type(caught.exception), BackendError)

    def test_start_requires_daemon(self):
        if daemon_probe().available:
            self.skipTest("daemon present; covered by daemon tests")
        with self.assertRaises(BackendUnavailableError):
            self.backend.start_job(make_spec("job-nodaemon"))

    def test_start_rejects_non_spec(self):
        with self.assertRaises(BackendError):
            self.backend.start_job({"job_id": "not-a-spec"})

    def test_record_qualification_requires_daemon(self):
        if daemon_probe().available:
            self.skipTest("daemon present; covered by daemon tests")
        with self.assertRaises(BackendError):
            self.backend.record_qualification(
                status="qualified", report_id="r", evidence={"a": 1})


# -- protocol probe scripts (worker_argv overrides for the stub daemon) ---

def _envelope(job_id, seq, msg_type, payload):
    return {"v": 1, "job_id": job_id, "seq": seq, "type": msg_type,
            "payload": payload}


def _emit_script(envelopes, *, tail=""):
    # Payloads travel base64: the -c code crosses TWO Windows command-line
    # serializations (backend Popen + stub exec), which corrupt embedded
    # double quotes. Single write + explicit flush (block-buffered pipes).
    import base64
    blob = base64.b64encode(json.dumps(envelopes).encode()).decode()
    return ("import json,sys,base64;"
            f"envs=json.loads(base64.b64decode('{blob}').decode());"
            "sys.stdout.write('\\n'.join(json.dumps(e,separators=(',',':'))"
            " for e in envs)+'\\n');"
            "sys.stdout.flush();" + tail)


def _raw_script(lines, *, tail=""):
    import base64
    blob = base64.b64encode(json.dumps(lines).encode()).decode()
    return ("import json,sys,base64;"
            "sys.stdout.write('\\n'.join(json.loads(base64.b64decode("
            f"'{blob}').decode()))+'\\n');"
            "sys.stdout.flush();" + tail)


_SLEEP_TAIL = "import time;time.sleep(30)"


# -- stub-daemon lifecycle tests (protocol/lifecycle only, NO isolation) --

class StubLifecycleTests(StubBackendMixin, unittest.TestCase):
    def test_full_refund_lifecycle_matches_local(self):
        backend = self.make_backend()
        self.start("job-life")
        backend.send("job-life", {"type": "hello", "payload": {}})
        events = backend.poll("job-life", 10000)
        self.assertEqual([e["type"] for e in events], ["started"])
        self.assertEqual(events[0]["payload"]["adapter"], REFUND_ADAPTER)
        self.assertEqual(self.backend.status("job-life")["state"], "running")
        backend.send("job-life", {"type": "reset",
                                 "payload": {"fixture": {}}})
        events = backend.poll("job-life", 10000)
        self.assertEqual([e["type"] for e in events], ["tool_result"])
        self.assertTrue(events[0]["payload"]["ok"])
        identity = {"order_id": "synthetic-order-1",
                    "customer_id": "synthetic-customer-1"}
        backend.send("job-life", {"type": "invoke",
                                 "payload": {"tool": "get_order",
                                             "arguments": dict(identity)}})
        events = backend.poll("job-life", 10000)
        self.assertTrue(events[0]["payload"]["ok"])
        backend.send("job-life", {"type": "invoke",
                                 "payload": {"tool": "issue_refund",
                                             "arguments": {
                                                 **identity,
                                                 "amount_minor": 250_000,
                                                 "idempotency_key": "b4-1"}}})
        events = backend.poll("job-life", 10000)
        self.assertTrue(events[0]["payload"]["ok"])
        backend.send("job-life", {"type": "evidence", "payload": {}})
        events = backend.poll("job-life", 10000)
        self.assertEqual(events[0]["type"], "evidence_chunk")
        backend.send("job-life", {"type": "snapshot", "payload": {}})
        events = backend.poll("job-life", 10000)
        self.assertTrue(events[0]["payload"]["ok"])
        backend.send("job-life", {"type": "set_fault",
                                 "payload": {"name": "timeout_after_commit",
                                             "params": {}}})
        events = backend.poll("job-life", 10000)
        self.assertTrue(events[0]["payload"]["ok"])
        backend.send("job-life", {"type": "clear_faults", "payload": {}})
        events = backend.poll("job-life", 10000)
        self.assertTrue(events[0]["payload"]["ok"])
        backend.send("job-life", {"type": "close", "payload": {}})
        events = backend.poll("job-life", 10000)
        self.assertEqual([e["type"] for e in events], ["completed"])
        self.assertEqual(backend.status("job-life")["state"], "completed")
        receipts = backend.close("job-life")
        self.assertEqual(receipts["state"], "completed")
        kinds = {r["kind"] for r in receipts["receipts"]}
        self.assertIn("lifecycle", kinds)
        self.assertIn("output", kinds)
        self.assertIn("container", kinds)
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "job-life")))
        with self.assertRaises(BackendError):
            backend.close("job-life")

    def test_start_validations(self):
        backend = self.make_backend()
        self.start("job-dup")
        with self.assertRaises(BackendError):
            backend.start_job(make_spec("job-dup"))
        with self.assertRaises(BackendError):
            backend.start_job(make_spec("job-unprepared", package={
                "kind": "tar.gz", "identity": "0" * 64}))
        with self.assertRaises(BackendError):
            backend.start_job(make_spec("job-badkind", package={
                "kind": "squashfs", "identity": "x"}))
        with self.assertRaises(BackendError):
            backend.send("job-dup", {"type": "teleport"})
        with self.assertRaises(BackendError):
            backend.send("job-dup", {"type": "hello", "payload": []})
        with self.assertRaises(BackendError):
            backend.send("job-dup", {"no-type": True})
        with self.assertRaises(BackendError):
            backend.poll("job-dup", -1)
        with self.assertRaises(BackendError):
            backend.cancel("job-dup", -1)
        status = backend.status("job-dup")
        self.assertEqual(set(status),
                         {"job_id", "state", "output_bytes", "elapsed_ms"})

    def test_send_after_terminal_refused(self):
        backend = self.make_backend()
        self.start("job-term")
        backend.send("job-term", {"type": "hello", "payload": {}})
        backend.poll("job-term", 10000)
        backend.send("job-term", {"type": "close", "payload": {}})
        backend.poll("job-term", 10000)
        with self.assertRaises(BackendError):
            backend.send("job-term", {"type": "hello", "payload": {}})

    def test_cancel_confirmed_and_state_removed(self):
        backend = self.make_backend()
        self.start("job-cancel")
        result = backend.cancel("job-cancel", 2000)
        self.assertEqual(result, {"state": "cancelled", "confirmed": True})
        leftovers = [f for f in os.listdir(self.state_dir)
                     if f.endswith(".json")]
        self.assertEqual(leftovers, [])
        receipts = backend.close("job-cancel")
        kinds = {r["kind"] for r in receipts["receipts"]}
        self.assertIn("termination", kinds)
        with self.assertRaises(BackendError):
            backend.cancel("job-cancel", 0)

    def test_cancel_escalates_past_term_ignorer(self):
        backend = self.make_backend(worker_argv=[
            sys.executable, "-c",
            "import signal,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"])
        self.start("job-escalate")
        result = backend.cancel("job-escalate", 200)
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["confirmed"])

    def test_wall_clock_deadline_marks_timed_out(self):
        backend = self.make_backend()
        self.start("job-deadline", wall_clock_ms=1)
        time.sleep(0.3)
        self.assertEqual(backend.status("job-deadline")["state"], "timed_out")
        self.assertEqual(backend.poll("job-deadline", 0), [])
        receipts = backend.close("job-deadline")
        self.assertEqual(receipts["state"], "timed_out")

    def test_output_cap_exhaustion(self):
        backend = self.make_backend(worker_argv=[
            sys.executable, "-c", _emit_script(
                [_envelope("job-cap", i, "tool_result",
                           {"ok": True, "pad": "x" * 400})
                 for i in range(5)], tail=_SLEEP_TAIL)])
        self.start("job-cap", max_output_bytes=1024)
        events = backend.poll("job-cap", 10000)
        types = [e["type"] for e in events]
        self.assertEqual(types[-1], "failed")
        self.assertEqual(events[-1]["payload"]["error"], "output-exhausted")
        self.assertEqual(backend.status("job-cap")["state"], "failed")

    def test_malformed_worker_output_rejected(self):
        backend = self.make_backend(worker_argv=[
            sys.executable, "-c",
            _raw_script(["this is not json"], tail=_SLEEP_TAIL)])
        self.start("job-malformed")
        events = backend.poll("job-malformed", 10000)
        self.assertEqual([e["type"] for e in events], ["failed"])
        self.assertEqual(events[0]["payload"]["error"],
                         "malformed-worker-output")

    def test_cross_job_output_rejected(self):
        backend = self.make_backend(worker_argv=[
            sys.executable, "-c", _emit_script(
                [_envelope("intruder", 0, "completed", {})],
                tail=_SLEEP_TAIL)])
        self.start("job-xjob")
        events = backend.poll("job-xjob", 10000)
        self.assertEqual([e["type"] for e in events], ["failed"])
        self.assertEqual(events[0]["payload"]["error"], "cross-job-output")

    def test_worker_seq_gap_rejected(self):
        backend = self.make_backend(worker_argv=[
            sys.executable, "-c", _emit_script(
                [_envelope("job-seq", 9, "completed", {})],
                tail=_SLEEP_TAIL)])
        self.start("job-seq")
        events = backend.poll("job-seq", 10000)
        self.assertEqual(events[0]["payload"]["error"], "worker-seq-gap")

    def test_unknown_worker_event_rejected(self):
        backend = self.make_backend(worker_argv=[
            sys.executable, "-c", _emit_script(
                [_envelope("job-unk", 0, "teleport", {})],
                tail=_SLEEP_TAIL)])
        self.start("job-unk")
        events = backend.poll("job-unk", 10000)
        self.assertEqual(events[0]["payload"]["error"],
                         "unknown-worker-event")

    def test_oversize_worker_line_rejected(self):
        # The long line is generated at runtime: embedding 70KB in argv
        # would exceed the OS command-line limit (WinError 206).
        code = ("import sys,time;sys.stdout.write('z'*70000+'\\n');"
                "sys.stdout.flush();time.sleep(30)")
        backend = self.make_backend(worker_argv=[sys.executable, "-c", code])
        self.start("job-big")
        events = backend.poll("job-big", 10000)
        self.assertEqual(events[0]["payload"]["error"],
                         "oversize-worker-line")

    def test_worker_early_exit_marked_failed(self):
        backend = self.make_backend(worker_argv=[
            sys.executable, "-c", "import sys;sys.exit(3)"])
        self.start("job-exit")
        self.assertEqual(backend.poll("job-exit", 10000), [])
        self.assertEqual(backend.status("job-exit")["state"], "failed")
        receipts = backend.close("job-exit")
        reasons = [r.get("reason") for r in receipts["receipts"]]
        self.assertIn("worker-exited", reasons)

    def test_worker_truncated_marks_failed(self):
        backend = self.make_backend(worker_argv=[
            sys.executable, "-c", _emit_script(
                [_envelope("job-trunc", 0, "truncated",
                           {"reason": "output-exhausted"})])])
        self.start("job-trunc")
        events = backend.poll("job-trunc", 10000)
        self.assertEqual([e["type"] for e in events], ["truncated"])
        self.assertEqual(backend.status("job-trunc")["state"], "failed")

    def test_archive_package_extracted_and_cleaned(self):
        backend = self.make_backend()
        prepared = backend.prepare({"kind": "tar.gz", "data": make_tar_gz(
            {"hello.txt": b"hi"})})
        self.start("job-arch", package={"kind": "tar.gz",
                                        "identity": prepared["identity"]})
        inputs_file = os.path.join(self.root, "job-arch", "inputs",
                                   "hello.txt")
        self.assertTrue(os.path.exists(inputs_file))
        backend.send("job-arch", {"type": "hello", "payload": {}})
        self.assertEqual(self.poll_types("job-arch"), ["started"])
        backend.close("job-arch")
        self.assertFalse(os.path.exists(os.path.join(self.root, "job-arch")))

    def test_sweep_removes_only_own_containers(self):
        backend = self.make_backend()
        self.start("job-sweep-a")
        self.start("job-sweep-b")
        survivor = os.path.join(self.state_dir, "survivor.json")
        with open(survivor, "w") as handle:
            handle.write("{}")
        result = backend.sweep_own_containers()
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["removed"]), 2)
        self.assertTrue(all(n.startswith("ghostlab-b4-")
                            for n in result["removed"]))
        self.assertTrue(os.path.exists(survivor))

    def test_down_mode_refuses_and_reports_blocked(self):
        backend = self.make_backend()
        os.environ["GHOSTLAB_B4_FAKE_MODE"] = "down"
        try:
            with self.assertRaises(BackendUnavailableError):
                backend.start_job(make_spec("job-down"))
            fresh = b4.DockerBackend(self.root,
                                     docker_exe=stub_docker_exe(),
                                     pythonpath=SRC_ROOT)
            self.assertEqual(fresh.capabilities()["qualification"]["status"],
                             "blocked-no-daemon")
        finally:
            os.environ.pop("GHOSTLAB_B4_FAKE_MODE", None)

    def test_qualification_record_plumbing_only(self):
        # A stub-recorded qualification proves file plumbing, never isolation.
        backend = self.make_backend()
        stored = backend.record_qualification(
            status="qualified", report_id="stub-r1",
            evidence={"note": "plumbing-only"})
        self.assertEqual(stored, {"status": "qualified",
                                  "report_id": "stub-r1"})
        caps = b4.DockerBackend(
            self.root, docker_exe=stub_docker_exe(),
            pythonpath=SRC_ROOT).capabilities()
        self.assertEqual(caps["qualification"],
                         {"status": "qualified", "report_id": "stub-r1"})
        other_image = b4.DockerBackend(
            self.root, docker_exe=stub_docker_exe(), image="other-image",
            pythonpath=SRC_ROOT).capabilities()
        self.assertEqual(other_image["qualification"]["status"], "unqualified")
        with self.assertRaises(BackendError):
            backend.record_qualification(status="bogus", report_id="r",
                                         evidence={"a": 1})
        with self.assertRaises(BackendError):
            backend.record_qualification(status="qualified", report_id="",
                                         evidence={"a": 1})


# -- daemon-enforced tests (BLOCKED unless a daemon is reachable) ---------
#
# Containment probes run INSIDE the job container via the worker_argv
# test hook under the SAME hardened flags/mounts as real jobs, and report
# back with a single protocol envelope. The usability run exercises the
# real worker + checked-in refund adapter end to end.

def _probe_code(body: str) -> str:
    return ("import json,os,sys;"
            "JOB=os.environ.get('GHOSTLAB_JOB_ID','');"
            "out={};"
            "try:\n" + body + "\n"
            "except Exception as _e:\n"
            " out['probe_error']=[type(_e).__name__,str(_e)[:300]];\n"
            "sys.stdout.write(json.dumps({'v':1,'job_id':JOB,'seq':0,"
            "'type':'completed','payload':out},"
            "separators=(',',':'))+'\\n');sys.stdout.flush()")


_CANARY_BODY = """
import errno
CANARY = {canary_path!r}
SECRET = {secret!r}
found = {{}}
for path in [CANARY, {sibling_path!r}, '/proc/self/environ']:
    try:
        with open(path, 'rb') as handle:
            data = handle.read(4096)
        try:
            text = data.decode('utf-8', 'replace')
        except Exception:
            text = ''
        found[path] = {{'read': True, 'secret_seen': SECRET in text}}
    except OSError as error:
        found[path] = {{'read': False, 'errno': errno.errorcode.get(
            error.errno, str(error.errno))}}
out['paths'] = found
"""

_EGRESS_BODY = """
import socket
TARGETS = {targets!r}
hits = {{}}
for host, port in TARGETS:
    try:
        sock = socket.create_connection((host, port), timeout=3)
    except OSError as error:
        hits['%s:%s' % (host, port)] = {{'connected': False,
                                          'error': str(error)[:120]}}
    else:
        try:
            sock.close()
        except OSError:
            pass
        hits['%s:%s' % (host, port)] = {{'connected': True}}
out['targets'] = hits
"""

_BREAKOUT_BODY = """
WRITES = {writes!r}
results = {{}}
for path in WRITES:
    try:
        with open(path, 'w') as handle:
            handle.write('pwned')
        results[path] = {{'wrote': True}}
    except OSError as error:
        results[path] = {{'wrote': False, 'error': str(error)[:120]}}
try:
    with open('/work/scratch/note.txt', 'w') as handle:
        handle.write('scratch-ok')
    results['/work/scratch/note.txt'] = {{'wrote': True}}
except OSError as error:
    results['/work/scratch/note.txt'] = {{'wrote': False,
                                          'error': str(error)[:120]}}
try:
    with open('/work/inputs/marker.txt', 'rb') as handle:
        results['inputs_marker'] = handle.read(64).decode('utf-8', 'replace')
except OSError as error:
    results['inputs_marker'] = 'ERROR:%s' % error
out['writes'] = results
"""

_FORK_BODY = """
import os as _os, time as _time
N = {count}
kids = []
fork_errors = 0
for _i in range(N):
    try:
        _pid = _os.fork()
    except OSError:
        fork_errors += 1
        continue
    if _pid == 0:
        try:
            _time.sleep(2)
        finally:
            _os._exit(0)
    kids.append(_pid)
deadline = _time.time() + 60
reaped = 0
while kids and _time.time() < deadline:
    try:
        _pid, _status = _os.waitpid(-1, _os.WNOHANG)
    except ChildProcessError:
        break
    if _pid == 0:
        _time.sleep(0.05)
    else:
        reaped += 1
        try:
            kids.remove(_pid)
        except ValueError:
            pass
out['spawned'] = reaped + len(kids)
out['fork_errors'] = fork_errors
out['requested'] = N
"""

_DISK_BODY = """
PATH = {path!r}
MB = {mb}
written = 0
full = False
chunk = b'\\0' * (1024 * 1024)
try:
    with open(PATH, 'wb') as handle:
        for _i in range(MB):
            try:
                handle.write(chunk)
            except OSError:
                full = True
                break
            written += 1
except OSError:
    full = True
out['written_mb'] = written
out['full'] = full
out['requested_mb'] = MB
"""

_ALLOC_BODY = """
MB = {mb}
blocks = []
for _i in range(MB):
    block = bytearray(1024 * 1024)
    for off in range(0, len(block), 4096):
        block[off] = 1
    blocks.append(block)
out['allocated_mb'] = len(blocks)
"""

_SLEEP_BODY = """
import signal as _signal, time as _time
try:
    _signal.signal(_signal.SIGTERM, _signal.SIG_IGN)
except OSError:
    pass
_time.sleep({seconds})
out['slept'] = True
"""


class DaemonEnforcedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        probe = daemon_probe()
        if not probe.available:
            raise unittest.SkipTest(f"no-daemon: {probe.error}")
        for source in ("worker.py", "protocol.py", "interface.py"):
            path = os.path.join(REPO_ROOT, "src", "ghostlab", "backends",
                                source)
            if not os.path.exists(path):
                raise unittest.SkipTest(
                    f"no-worker-sources: missing {source}")
        cls.exe = probe.docker_exe
        cls.workdir = tempfile.mkdtemp(prefix="ghostlab-b4-daemon-")
        cls.root = os.path.join(cls.workdir, "workspaces")
        os.makedirs(cls.root, exist_ok=True)
        cls.tag = f"ghostlab-b4-test-{uuid.uuid4().hex[:8]}"
        build = subprocess.run(
            [cls.exe, "build", "-f",
             "src/ghostlab/backends/Dockerfile.worker", "-t", cls.tag, "."],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=900)
        if build.returncode != 0:
            tail = ((build.stdout or "") + (build.stderr or ""))[-2000:]
            raise unittest.SkipTest(f"no-worker-build: {tail}")
        cls.image_id = cls.inspect_id(cls.tag)
        print(f"\n[B4] worker image: {cls.tag} id={cls.image_id}")

    @classmethod
    def tearDownClass(cls):
        try:
            subprocess.run([cls.exe, "rmi", "-f", cls.tag],
                           capture_output=True, text=True, timeout=120)
            leftovers = subprocess.run(
                [cls.exe, "ps", "-aq", "--filter", "name=ghostlab-b4-"],
                capture_output=True, text=True, timeout=60)
            print(f"[B4] leftover own containers: "
                  f"{(leftovers.stdout or '').strip() or 'none'}")
        finally:
            shutil.rmtree(cls.workdir, ignore_errors=True)
            super().tearDownClass()

    @classmethod
    def inspect_id(cls, ref):
        completed = subprocess.run(
            [cls.exe, "inspect", "--format", "{{.Id}}", ref],
            capture_output=True, text=True, timeout=60)
        if completed.returncode != 0:
            raise AssertionError(
                f"inspect failed: {(completed.stderr or '').strip()}")
        return (completed.stdout or "").strip()

    def setUp(self):
        super().setUp()
        self.open_jobs: list[tuple] = []

    def tearDown(self):
        for backend, job_id in self.open_jobs:
            try:
                backend.close(job_id)
            except BackendError:
                pass
        try:
            b4.DockerBackend(self.root,
                             docker_exe=self.exe).sweep_own_containers()
        except BackendError:
            pass
        super().tearDown()

    def make_backend(self, **overrides):
        params = {"docker_exe": self.exe, "image": self.tag}
        params.update(overrides)
        return b4.DockerBackend(self.root, **params)

    def start(self, backend, job_id, **spec_over):
        spec = make_spec(job_id, **spec_over)
        backend.start_job(spec)
        self.open_jobs.append((backend, job_id))
        return spec

    def run_probe(self, job_id, body, *, wall_clock_ms=120000):
        backend = self.make_backend(
            worker_argv=["python", "-c", _probe_code(body)])
        self.start(backend, job_id, wall_clock_ms=wall_clock_ms,
                   max_output_bytes=65536)
        events = backend.poll(job_id, 120000)
        self.assertEqual([e["type"] for e in events], ["completed"],
                         f"probe events: {events}")
        self.assertNotIn("probe_error", events[0]["payload"],
                         f"probe crashed: {events[0]['payload']}")
        findings = events[0]["payload"]
        backend.close(job_id)
        return findings

    def test_worker_image_digest_recorded(self):
        digest = self.image_id
        print(f"\n[B4] worker image pin: {digest}")
        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual(len(digest), len("sha256:") + 64)

    def test_usability_refund_lifecycle(self):
        backend = self.make_backend()
        self.start("job-use", wall_clock_ms=180000)
        backend.send("job-use", {"type": "hello", "payload": {}})
        events = backend.poll("job-use", 60000)
        self.assertEqual([e["type"] for e in events], ["started"])
        backend.send("job-use", {"type": "reset",
                                 "payload": {"fixture": {}}})
        events = backend.poll("job-use", 60000)
        self.assertTrue(events[0]["payload"]["ok"])
        identity = {"order_id": "synthetic-order-1",
                    "customer_id": "synthetic-customer-1"}
        backend.send("job-use", {"type": "invoke",
                                 "payload": {"tool": "get_order",
                                             "arguments": dict(identity)}})
        events = backend.poll("job-use", 60000)
        self.assertTrue(events[0]["payload"]["ok"])
        backend.send("job-use", {"type": "evidence", "payload": {}})
        events = backend.poll("job-use", 60000)
        self.assertEqual(events[0]["type"], "evidence_chunk")
        backend.send("job-use", {"type": "snapshot", "payload": {}})
        events = backend.poll("job-use", 60000)
        self.assertTrue(events[0]["payload"]["ok"])
        backend.send("job-use", {"type": "close", "payload": {}})
        events = backend.poll("job-use", 60000)
        self.assertEqual([e["type"] for e in events], ["completed"])
        receipts = backend.close("job-use")
        self.assertEqual(receipts["state"], "completed")
        gone = subprocess.run([self.exe, "inspect", "--format", "{{.Id}}",
                               f"ghostlab-b4-job-use"],
                              capture_output=True, text=True, timeout=30)
        self.assertNotEqual(gone.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "job-use")))

    def test_adversarial_profile_and_qualification(self):
        secret = f"b4canary-{uuid.uuid4().hex}"
        canary = os.path.join(self.workdir, f"canary-{uuid.uuid4().hex}.txt")
        with open(canary, "w") as handle:
            handle.write(secret)
        # A sibling job whose inputs must stay unreadable from other jobs.
        sibling = self.make_backend(worker_argv=[
            "python", "-c", _probe_code(_SLEEP_BODY.format(seconds=120))])
        self.start(sibling, "job-sibling", wall_clock_ms=180000)
        sibling_inputs = os.path.join(self.root, "job-sibling", "inputs")
        with open(os.path.join(sibling_inputs, "sibling-secret.txt"),
                  "w") as handle:
            handle.write("sibling-private")

        seen_payloads: list = []
        with self.subTest("canaries unread"):
            findings = self.run_probe(
                "job-canary", _CANARY_BODY.format(
                    canary_path=canary, secret=secret,
                    sibling_path=os.path.join(sibling_inputs,
                                              "sibling-secret.txt")))
            seen_payloads.append(findings)
            paths = findings["paths"]
            self.assertFalse(paths[canary]["read"])
            self.assertFalse(paths[os.path.join(
                sibling_inputs, "sibling-secret.txt")]["read"])
            for info in paths.values():
                self.assertFalse(info.get("secret_seen", False))
        with self.subTest("zero egress hits"):
            findings = self.run_probe(
                "job-egress", _EGRESS_BODY.format(targets=[
                    ("127.0.0.1", 9), ("169.254.169.254", 80),
                    ("8.8.8.8", 53)]))
            seen_payloads.append(findings)
            for label, info in findings["targets"].items():
                self.assertFalse(info["connected"], label)
        with self.subTest("breakout writes absent"):
            pwn = f"pwned-{uuid.uuid4().hex}"
            backend = self.make_backend(worker_argv=[
                "python", "-c", _probe_code(_BREAKOUT_BODY.format(writes=[
                    f"/{pwn}", "/work/inputs/evil.txt",
                    "/work/scratch/../../evil2.txt"]))])
            prepared = backend.prepare({"kind": "tar.gz", "data": make_tar_gz(
                {"marker.txt": b"own-inputs"})})
            self.start(backend, "job-break",
                       package={"kind": "tar.gz",
                                "identity": prepared["identity"]},
                       wall_clock_ms=120000)
            events = backend.poll("job-break", 120000)
            self.assertEqual([e["type"] for e in events], ["completed"])
            findings = events[0]["payload"]
            seen_payloads.append(findings)
            writes = findings["writes"]
            self.assertFalse(writes[f"/{pwn}"]["wrote"])
            self.assertFalse(writes["/work/inputs/evil.txt"]["wrote"])
            self.assertFalse(writes["/work/scratch/../../evil2.txt"]["wrote"])
            self.assertTrue(writes["/work/scratch/note.txt"]["wrote"])
            self.assertEqual(writes["inputs_marker"], "own-inputs")
            backend.close("job-break")
            stray = []
            for base, _, files in os.walk(self.workdir):
                for name in files:
                    if "pwned" in name or name in ("evil.txt", "evil2.txt"):
                        stray.append(os.path.join(base, name))
            self.assertEqual(stray, [])
        with self.subTest("fork bomb bounded"):
            findings = self.run_probe(
                "job-fork", _FORK_BODY.format(count=200))
            seen_payloads.append(findings)
            total = findings["spawned"] + findings["fork_errors"]
            self.assertEqual(total, findings["requested"])
            self.assertGreater(findings["fork_errors"], 0)
        with self.subTest("disk bomb bounded"):
            findings = self.run_probe(
                "job-disk", _DISK_BODY.format(path="/work/scratch/blob",
                                              mb=128))
            seen_payloads.append(findings)
            self.assertTrue(findings["full"])
            self.assertLess(findings["written_mb"], 128)
            self.assertGreaterEqual(findings["written_mb"], 32)
        with self.subTest("memory bomb contained"):
            backend = self.make_backend(worker_argv=[
                "python", "-c", _probe_code(_ALLOC_BODY.format(mb=1024))])
            self.start(backend, "job-oom", wall_clock_ms=120000)
            events = backend.poll("job-oom", 120000)
            if events and events[0]["type"] == "completed":
                self.assertIn("MemoryError", json.dumps(
                    events[0]["payload"]))
            else:
                self.assertEqual(backend.status("job-oom")["state"], "failed")
            seen_payloads.append(
                {"oom": [e["type"] for e in events]})
            receipts = backend.close("job-oom")
            self.assertIn(receipts["state"], ("failed", "completed"))
        with open(canary) as handle:
            self.assertEqual(handle.read(), secret)
        self.assertNotIn(secret, json.dumps(seen_payloads))
        sibling.close("job-sibling")

        evidence = {"canary": "unread", "egress": "zero-hits",
                    "breakout": "absent", "fork": "bounded-by-pids-limit",
                    "disk": "bounded-by-tmpfs", "memory": "contained",
                    "image": self.tag, "image_id": self.image_id}
        report_id = ("b4-adversarial-"
                     + time.strftime("%Y%m%d", time.gmtime())
                     + f"-{self.tag.rsplit('-', 1)[-1]}")
        stored = self.make_backend().record_qualification(
            status="qualified", report_id=report_id, evidence=evidence)
        self.assertEqual(stored["report_id"], report_id)
        caps = self.make_backend().capabilities()
        self.assertEqual(caps["qualification"],
                         {"status": "qualified", "report_id": report_id})
        print(f"\n[B4] qualification: {caps['qualification']}")

    def test_cancel_confirmed_against_ignoring_worker(self):
        backend = self.make_backend(worker_argv=[
            "python", "-c", _probe_code(_SLEEP_BODY.format(seconds=120))])
        self.start(backend, "job-kill", wall_clock_ms=180000)
        time.sleep(5)
        started = time.monotonic()
        result = backend.cancel("job-kill", 2000)
        elapsed = time.monotonic() - started
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["confirmed"])
        self.assertLess(elapsed, 30)
        gone = subprocess.run(
            [self.exe, "ps", "-aq", "--filter", "name=ghostlab-b4-job-kill"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual((gone.stdout or "").strip(), "")
        backend.close("job-kill")

    def test_forged_worker_output_rejected(self):
        # Honest forgery: a complete envelope carrying another job's id.
        code = ("import json,sys;"
                "sys.stdout.write(json.dumps({'v':1,'job_id':'forged-job',"
                "'seq':0,'type':'completed','payload':{}},"
                "separators=(',',':'))+'\\n');sys.stdout.flush()")
        backend = self.make_backend(
            worker_argv=["python", "-c", code])
        self.start(backend, "job-forge", wall_clock_ms=60000)
        events = backend.poll("job-forge", 60000)
        self.assertEqual([e["type"] for e in events], ["failed"])
        self.assertEqual(events[0]["payload"]["error"], "cross-job-output")
        backend.close("job-forge")
