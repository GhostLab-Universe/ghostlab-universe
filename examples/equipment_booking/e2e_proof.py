"""External end-to-end proof: BOOKING through the shared testing pipeline.

Runnable script (exit 0 iff every stage passes; JSON summary on stdout)::

    register booking-env (explicit allowlist) -> import manifest -> qualify
    -> register target agents -> resolve supported conditions -> execute naive
    via the controller -> minimize the violating trace -> fresh-test corrected
    -> export guards -> run each guard under pytest (naive FAILS, corrected
    PASSES).

Everything flows through the driver registry: the central controller needs
no booking-specific code (proven by grepping its source for booking tool
names). Zero model calls, no secrets, stdlib first. Track-private temp DBs
only; guards + summary go under ``--out`` (system temp by default).

This module is also imported by the exported guards (adapter + check_fn), so
its top level is strictly side-effect-free: path bootstrap, imports, defs.
"""

import argparse
import gc
import json
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.equipment_booking import driver_booking  # noqa: E402
from examples.equipment_booking.checks import verify_equipment  # noqa: E402
from examples.equipment_booking.env_adapter import (  # noqa: E402
    BOOKING_MODES, BOOKING_TOOLS, BookingEnvAdapter, ENV_ID, ENV_VERSION)
from examples.equipment_booking.world import (  # noqa: E402
    EquipmentFixture, ToolError as BookingToolError)
from ghostlab.core import ToolError as CoreToolError  # noqa: E402
from ghostlab.envs.manifest import import_manifest  # noqa: E402
from ghostlab.envs.qualification import run_qualification  # noqa: E402
from ghostlab.envs.registry import EnvRegistry  # noqa: E402
from ghostlab.experiments import checks as check_registry  # noqa: E402
from ghostlab.experiments import drivers as driver_registry  # noqa: E402
from ghostlab.experiments import ops  # noqa: E402
from ghostlab.experiments.checks import CheckError  # noqa: E402
from ghostlab.experiments.configs import ConfigError  # noqa: E402
from ghostlab.experiments.minimize import (  # noqa: E402
    minimize_prefix, replay_actions)
from ghostlab.experiments.regress import export_guard_py  # noqa: E402
from ghostlab.experiments.store import ExperimentStore  # noqa: E402
from ghostlab.targets import AgentManifest, TargetRegistry  # noqa: E402

PROJECT_ID = "proj-t4"
BOOKING_REF = "examples.equipment_booking.env_adapter:BookingEnvAdapter"
ADAPTER_REF = "examples.equipment_booking.e2e_proof:BookingReplayAdapter"
CHECK_FN = "examples.equipment_booking.e2e_proof.booking_prefix_violated"
ALLOWLIST = ("ghostlab.", "examples.equipment_booking.")
BOOKING_FIXTURE = {"equipment_id": "synthetic-microscope-1",
                   "user_id": "synthetic-user-1",
                   "slot": "2026-11-03T09:00Z"}
BOOKING_LIMITS = {"max_tool_calls": 20, "max_total_tool_calls": 200,
                  "max_wall_clock_ms": 60000}
BOOKING_RULES = list(driver_booking.CHECK_IDS)
BOOKING_FAULTS = [{"hook": "timeout_after_commit", "params": {},
                   "required": True}]

TREE_ADAPTATIONS = [
    "booking resolves through public ops.create_requested_config +"
    " ops.resolve_config (owner-generalized validation dispatches fixture,"
    " rules and required tools per registered env driver); no hand-crafted"
    " resolved records anywhere in this proof.",
    "the booking naive trace is already minimal (3 actions; every shorter"
    " leading prefix passes), so minimize_prefix yields dropped=0 with"
    " stable=True and the violation preserved.",
    "the example BookingTimeout is not a core ToolError, so guard replay uses"
    " the importable BookingReplayAdapter below, which translates recorded"
    " world errors to core ToolError for shared minimize/guard replay"
    " semantics; trace evidence keeps the original BookingTimeout outcome.",
]


class BookingReplayAdapter:
    """BookingEnvAdapter with core-ToolError replay semantics.

    :func:`minimize.replay_actions` continues past recorded world outcomes
    that raise ``ghostlab.core.ToolError``; the example world raises its own
    ``BookingTimeout`` (not a core ToolError). The world records the outcome
    in its trace before raising, so translating the propagated error
    preserves evidence while letting shared replay/minimize/guard code run
    unchanged.
    """

    def __init__(self) -> None:
        self._inner = BookingEnvAdapter()

    def reset(self, fixture: dict) -> None:
        return self._inner.reset(fixture)

    def invoke(self, tool: str, arguments: dict) -> dict:
        try:
            return self._inner.invoke(tool, arguments)
        except BookingToolError as error:
            raise CoreToolError(str(error)) from error

    def evidence(self) -> dict:
        return self._inner.evidence()


def booking_prefix_violated(evidence) -> bool:
    """Whether replay evidence violates any booking invariant.

    Guard check_fn: ``(evidence) -> bool``. The fixture is recovered from
    the snapshot's embedded equipment (mirrors
    ``minimize.refund_rules_violated``, which recovers it from the order).
    """
    if type(evidence) is not dict:
        raise ValueError("evidence must be an object")
    snapshot = evidence.get("snapshot")
    if type(snapshot) is not dict \
            or type(snapshot.get("equipment")) is not dict:
        raise ValueError("evidence snapshot must embed the equipment fixture")
    result = verify_equipment(EquipmentFixture(**snapshot["equipment"]),
                              snapshot)
    return not all(result.values())


def _fail(stage: str, message: str) -> AssertionError:
    return AssertionError(f"[{stage}] {message}")


def _register_driver_and_checks() -> None:
    # Injected registration: driver_booking keeps relative-only imports, so
    # the importer registers its driver + checks. Core (ops -> controller ->
    # drivers_refund) already self-registered the refund driver + checks, so
    # the shared unique_idempotency_keys id is skipped here (same as T2).
    if driver_registry.get_driver(driver_booking.ENV_ID) is None:
        driver_registry.register_driver(driver_booking.ENV_ID, driver_booking)
    for spec in driver_booking.BOOKING_CHECK_SPECS:
        try:
            check_registry.get_check(spec["check_id"])
        except CheckError:
            check_registry.register_check(**spec)


def _booking_manifest() -> dict:
    return {
        "manifest_version": 1, "env_id": ENV_ID, "env_version": ENV_VERSION,
        "adapter_ref": BOOKING_REF,
        "display_name": "Synthetic equipment booking",
        "tools": deepcopy(BOOKING_TOOLS),
        "reproduction_modes": list(BOOKING_MODES),
        "fault_hooks": ["timeout_after_commit"],
        "notes": "T4 end-to-end proof: external booking env through the"
                 " shared register/manifest/qualify/resolve/execute pipeline.",
    }


def _booking_requested(agent: str) -> dict:
    return {"config_version": 1, "name": f"booking {agent} proof",
            "env_id": ENV_ID, "agent": agent,
            "fixture": dict(BOOKING_FIXTURE),
            "faults": deepcopy(BOOKING_FAULTS),
            "rules": list(BOOKING_RULES),
            "limits": dict(BOOKING_LIMITS),
            "repetitions": {"count": 1}}


def _run_guard_pytest(guard_path: Path) -> dict:
    cmd = [sys.executable, "-m", "pytest", str(guard_path), "-q",
           "--no-header", "-p", "no:cacheprovider"]
    env = dict(os.environ)
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"]
                               if env.get("PYTHONPATH") else "")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                          text=True, timeout=300)
    combined = (proc.stdout or "") + (proc.stderr or "")
    return {"returncode": proc.returncode, "tail": combined[-2000:]}


def run_proof(out_dir: Path) -> dict:
    """Execute every pipeline stage; raise AssertionError on any failure."""
    stages: dict = {}

    # Stage 0: no domain edits to the central controller.
    text = (ROOT / "src" / "ghostlab" / "experiments"
            / "controller.py").read_text(encoding="utf-8")
    grep = {"book_equipment": text.count("book_equipment"),
            "get_booking_status": text.count("get_booking_status")}
    if grep["book_equipment"] or grep["get_booking_status"]:
        raise _fail("controller-grep",
                    f"controller.py names booking tools: {grep}")
    stages["controller_grep"] = grep

    base = tempfile.mkdtemp(prefix="t4-e2e-db-")
    try:
        registry = EnvRegistry(Path(base) / "env.sqlite",
                               allowed_adapter_prefixes=ALLOWLIST)

        # Stage 1: register booking-env with an explicit allowlist.
        registration = registry.register(
            env_id=ENV_ID, env_version=ENV_VERSION,
            adapter_ref=BOOKING_REF,
            display_name="Synthetic equipment booking",
            declared_tools=deepcopy(BOOKING_TOOLS),
            declared_modes=list(BOOKING_MODES),
            registered_by="t4-proof")
        stages["register"] = {"env_id": registration["env_id"],
                              "adapter_ref": registration["adapter_ref"],
                              "allowlist": list(ALLOWLIST)}

        # Stage 2: import the env manifest.
        imported = import_manifest(
            registry, json.dumps(_booking_manifest()).encode("utf-8"))
        stages["manifest"] = {
            "manifest_digest": imported["manifest_digest"]}

        # Stage 3: qualify against the declared capabilities.
        _register_driver_and_checks()
        report = run_qualification(registry, ENV_ID, manifest_digest=
                                   imported["manifest_digest"],
                                   fixture=dict(BOOKING_FIXTURE))
        qual_checks = [{"name": c["name"], "passed": c["passed"]}
                       for c in report["checks"]]
        if report["status"] != "qualified-for-declared":
            raise _fail("qualify", f"unexpected status {report['status']}:"
                                   f" {qual_checks}")
        stages["qualify"] = {"status": report["status"],
                             "checks": qual_checks}

        # Stage 4: register the two executed target agents.
        targets = TargetRegistry(Path(base) / "targets.sqlite")
        for name, driver_agent, retry in (
                ("booking-naive", "vulnerable", "fresh-key retry"),
                ("booking-corrected", "repaired", "stable-key recheck")):
            targets.register(AgentManifest(
                name=name, version="1",
                config={"driver_agent": driver_agent, "env_id": ENV_ID,
                        "procedure": "examples.equipment_booking."
                                     "driver_booking.drive",
                        "fixture": dict(BOOKING_FIXTURE)},
                allowed_tools=("get_equipment", "book_equipment",
                               "get_booking_status"),
                entry=f"driver_booking.drive({driver_agent})",
                description=f"Booking {driver_agent} agent: {retry} after a"
                            " timeout-after-commit."))
        listed = sorted(a.name for a in targets.list())
        if listed != ["booking-corrected", "booking-naive"]:
            raise _fail("targets", f"unexpected agent list {listed}")
        if targets.get("booking-naive") is None \
                or targets.get("booking-corrected") is None:
            raise _fail("targets", "registered agents do not round-trip")
        stages["targets"] = {"agents": listed}

        # Stage 5: resolve supported conditions through public ops.
        policy = {"policy_version": "policy/1",
                  "ceiling_tool_calls": 20,
                  "ceiling_total_tool_calls": 200,
                  "ceiling_repetitions": 5,
                  "ceiling_wall_clock_ms": 60000,
                  "allowed_agents": ["vulnerable", "repaired"],
                  "allowed_hooks": ["timeout_after_commit"]}
        store = ExperimentStore(Path(base) / "exp.sqlite")
        bogus_body = _booking_requested("vulnerable")
        bogus_body["rules"] = ["no_such_check_t4"]
        try:
            ops.create_requested_config(store, PROJECT_ID, bogus_body,
                                        policy, "t4-proof")
        except ConfigError as error:
            unsupported_rejected = "unique known" in str(error)
        else:
            unsupported_rejected = False
        if not unsupported_rejected:
            raise _fail("resolve", "bogus check was not rejected as"
                                   " unsupported for booking-env")
        declared = set(driver_registry.get_driver(ENV_ID).declared_checks())
        if not set(BOOKING_RULES) <= declared:
            raise _fail("resolve", "booking rules fall outside"
                                   f" declared_checks: {sorted(declared)}")

        def _resolve(agent: str) -> dict:
            created = ops.create_requested_config(
                store, PROJECT_ID, _booking_requested(agent), policy,
                "t4-proof")
            return ops.resolve_config(store, registry, PROJECT_ID,
                                      created["config_id"], policy,
                                      "t4-proof")

        naive_resolved = _resolve("vulnerable")
        corrected_resolved = _resolve("repaired")
        stages["resolve"] = {
            "unsupported_rejected": True,
            "naive_resolved_id": naive_resolved["resolved_id"],
            "corrected_resolved_id": corrected_resolved["resolved_id"]}

        # Stage 6: execute the naive agent via the controller.
        naive_run = ops.start_run(store, registry, PROJECT_ID,
                                  naive_resolved["resolved_id"])
        naive = naive_run["run"]
        timeout_ids = [e.get("event_id") for e in naive["evidence"]["trace"]
                       if isinstance(e, dict)
                       and isinstance(e.get("outcome"), dict)
                       and e["outcome"].get("error") == "BookingTimeout"]
        snapshot = naive["evidence"]["snapshot"]
        keys = [r.get("idempotency_key")
                for r in snapshot.get("reservations", [])]
        if naive["status"] != "completed" or naive["verdict"] != "fail":
            raise _fail("naive-run", f"expected completed/fail, got"
                                     f" {naive['status']}/{naive['verdict']}")
        if naive["rules_failed"] != ["no_duplicate_booking"]:
            raise _fail("naive-run",
                        f"expected duplicate failure, got {naive['checks']}")
        if not timeout_ids or snapshot.get("total_bookings") != 2:
            raise _fail("naive-run", "missing cited event/state evidence"
                                     f" (timeout events {timeout_ids},"
                                     f" bookings {snapshot.get('total_bookings')})")
        if naive["model_calls"] != 0 or not naive["faults"][0]["fired"]:
            raise _fail("naive-run", "expected zero model calls and a fired"
                                     " required fault")
        stages["naive_run"] = {
            "run_id": naive_run["run_id"], "status": naive["status"],
            "verdict": naive["verdict"], "model_calls": naive["model_calls"],
            "tool_calls": naive["tool_calls"], "checks": naive["checks"],
            "rules_failed": naive["rules_failed"], "fault_fired": True,
            "timeout_event_id": timeout_ids[0], "total_bookings": 2,
            "reservation_keys": keys}

        # Stage 7: minimize the violating trace.
        actions = [{"tool": e["request"]["tool"],
                    "arguments": dict(e["request"]["arguments"])}
                   for e in naive["evidence"]["trace"]]
        minimized = minimize_prefix(BookingReplayAdapter,
                                    dict(BOOKING_FIXTURE), actions,
                                    booking_prefix_violated)
        required = minimized["required_prefix"]
        confirm = replay_actions(BookingReplayAdapter(),
                                 dict(BOOKING_FIXTURE), required)
        required_violates = booking_prefix_violated(confirm) is True
        if not minimized["stable"] or not required_violates:
            raise _fail("minimize", f"unstable or clean prefix: {minimized}")
        if len(required) > len(actions):
            raise _fail("minimize", "minimized prefix grew beyond the trace")
        stages["minimize"] = {
            "full_actions": len(actions), "required_actions": len(required),
            "dropped": minimized["dropped"], "stable": True,
            "required_violates": True, "replays": minimized["replays"]}

        # Stage 8: fresh-test the corrected agent (new run, new run id).
        fixed_run = ops.start_run(store, registry, PROJECT_ID,
                                  corrected_resolved["resolved_id"])
        fixed = fixed_run["run"]
        if fixed["verdict"] != "pass" or fixed["rules_failed"] != []:
            raise _fail("corrected-run",
                        f"expected pass, got {fixed['verdict']}:"
                        f" {fixed['checks']}")
        if fixed_run["run_id"] == naive_run["run_id"]:
            raise _fail("corrected-run", "corrected run reused the naive id")
        if fixed["model_calls"] != 0 or not fixed["faults"][0]["fired"]:
            raise _fail("corrected-run", "expected zero model calls and a"
                                         " fired required fault")
        stages["corrected_run"] = {
            "run_id": fixed_run["run_id"], "verdict": fixed["verdict"],
            "rules_failed": [], "model_calls": fixed["model_calls"],
            "fault_fired": True}
        fixed_actions = [{"tool": e["request"]["tool"],
                          "arguments": dict(e["request"]["arguments"])}
                         for e in fixed["evidence"]["trace"]]
        fixed_evidence = replay_actions(BookingReplayAdapter(),
                                        dict(BOOKING_FIXTURE), fixed_actions)
        if booking_prefix_violated(fixed_evidence) is not False:
            raise _fail("corrected-run", "corrected prefix still violates")

        # Stage 9: export one guard per behavior, deps named explicitly.
        guards_dir = out_dir / "guards"
        naive_guard = Path(export_guard_py(
            ADAPTER_REF, dict(BOOKING_FIXTURE), required, CHECK_FN,
            guards_dir / "test_guard_booking_naive_t4.py"))
        fixed_guard = Path(export_guard_py(
            ADAPTER_REF, dict(BOOKING_FIXTURE), fixed_actions, CHECK_FN,
            guards_dir / "test_guard_booking_corrected_t4.py"))
        for path in (naive_guard, fixed_guard):
            guard_text = path.read_text(encoding="utf-8")
            if ADAPTER_REF not in guard_text or CHECK_FN not in guard_text:
                raise _fail("export-guard",
                            f"{path.name} does not name its deps explicitly")

        # Stage 10: run the exported guards: naive FAILS, corrected PASSES.
        naive_pytest = _run_guard_pytest(naive_guard)
        if naive_pytest["returncode"] != 1 \
                or "1 failed" not in (naive_pytest["tail"] or ""):
            raise _fail("guard-naive", "naive guard did not fail as a caught"
                                       f" bug: {naive_pytest}")
        fixed_pytest = _run_guard_pytest(fixed_guard)
        if fixed_pytest["returncode"] != 0 \
                or "1 passed" not in (fixed_pytest["tail"] or ""):
            raise _fail("guard-corrected", "corrected guard did not pass:"
                                           f" {fixed_pytest}")
        stages["guards"] = {
            "adapter_ref": ADAPTER_REF, "check_fn": CHECK_FN,
            "naive_guard": str(naive_guard.relative_to(out_dir)),
            "corrected_guard": str(fixed_guard.relative_to(out_dir)),
            "naive_pytest": naive_pytest, "corrected_pytest": fixed_pytest}
    finally:
        gc.collect()

    return {"ok": True, "out_dir": str(out_dir), "project_id": PROJECT_ID,
            "stages": stages, "tree_adaptations": list(TREE_ADAPTATIONS)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Booking end-to-end proof through the shared pipeline.")
    parser.add_argument("--out", default=None,
                        help="directory for guards + summary.json"
                             " (system-temp default)")
    args = parser.parse_args(argv)
    out_dir = Path(args.out) if args.out \
        else Path(tempfile.mkdtemp(prefix="t4-e2e-out-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = run_proof(out_dir)
    except Exception as error:  # proof contract: JSON out, nonzero exit
        summary = {"ok": False, "out_dir": str(out_dir),
                   "project_id": PROJECT_ID,
                   "error": f"{type(error).__name__}: {error}",
                   "tree_adaptations": list(TREE_ADAPTATIONS)}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2),
                                          encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
