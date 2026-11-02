"""T1 test-export: generator plus the runnable modules it emits.

Builds REAL regression cases (refund/journey minimization, ext
minimization on the refund world), exports pytest modules, runs them as
subprocesses, and proves both directions: exports pass on this tree and
fail meaningfully when the defined violation recurs.
"""

import hashlib
import importlib.util
import itertools
import json
import re
import socket
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from ghostlab import capsule as refund_capsule
from ghostlab.adapters import register_refund_adapters
from ghostlab.core import Fixture, Scenario, run as refund_run
from ghostlab.diagnostics.minimize import minimize_capsule
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.replay.export_tests import export_pytest_module, main
from ghostlab.replay.minimize_ext import RunEvidence, minimize_faults
from ghostlab.replay.recorded import check_verdicts
from ghostlab.worlds import journey_capsule
from ghostlab.worlds.journey import JourneyFixture, JourneyScenario, scenario_json
from ghostlab.worlds.journey_run import run as journey_run

REFUND_INVARIANT = "total_refund_within_payment"
PRIVACY = "delivered_messages_permitted"
TIMEOUT = "timeout_after_commit"

SCRIPT = str(Path(__file__).resolve().parents[2] / "scripts"
             / "export_regression_tests.py")
_counter = itertools.count()

FORBIDDEN = re.compile(
    r"(?i)(api[_-]?key|apikey|secret|passwd|password|bearer|authorization"
    r"|access[_-]token|auth[_-]token|private[_-]key|client[_-]secret"
    r"|credentials|BASETEN|MODEL_API_KEY|ANTHROPIC|OPENAI)")
FORBIDDEN_RUNTIME = re.compile(
    r"(?i)\b(socket|urllib|requests|httpx|subprocess|os\.environ|getenv)\b")


def _write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _refund_violation(tmp_path):
    """A real minimized refund violation case plus its directory."""
    tag = f"refund-{next(_counter)}"
    fixture, scenario = Fixture(), Scenario(timeout_after_commit=True)
    source = tmp_path / f"{tag}-src.json"
    refund_capsule.export_capsule(
        source, fixture, scenario, refund_run(fixture, scenario, "vulnerable"))
    out = tmp_path / f"{tag}-reduced"
    report = minimize_capsule(source, out, invariant=REFUND_INVARIANT)
    assert report["status"] == "already_minimal"
    return out / "regression-case.json"


def _journey_violation(tmp_path):
    """A real minimized journey privacy case plus its directory."""
    tag = f"journey-{next(_counter)}"
    fixture = JourneyFixture()
    data = scenario_json([
        ("stale_preference_memory", None, {"preference_version": 1}),
        ("delayed_itinerary_update", None, {"delay_events": 2}),
        ("sharing_revoked_while_queued", None, {"recipient_id": "participant-c"})])
    recorded = journey_run(fixture, JourneyScenario.from_json(data), "journey-naive")
    source = tmp_path / f"{tag}-src.json"
    journey_capsule.export_capsule(source, fixture, data, recorded)
    out = tmp_path / f"{tag}-reduced"
    report = minimize_capsule(source, out, invariant=PRIVACY)
    assert report["status"] == "reduced"
    return out / "regression-case.json"


def _fixed_case(tmp_path):
    """A fixed-behavior /2 case: repaired adapter, invariant holds."""
    fixture, scenario = Fixture(), Scenario(timeout_after_commit=True)
    run = refund_run(fixture, scenario, "repaired")
    assert all(run["checks"].values())
    capsule_path = tmp_path / "fixed-capsule.json"
    refund_capsule.export_capsule(capsule_path, fixture, scenario, run)
    case_path = tmp_path / "fixed-case.json"
    _write_json(case_path, {
        "schema": "ghostlab-diagnostic-regression/2",
        "capsule": capsule_path.name,
        "sha256": hashlib.sha256(capsule_path.read_bytes()).hexdigest(),
        "invariant": REFUND_INVARIANT, "expected_passed": True})
    return case_path


def _ext_pair(tmp_path):
    """A real ext regression dict plus consistent recorded evidence."""
    registry = register_refund_adapters()
    fixture = Fixture()
    captured = {}

    def run(subset):
        scenario = {"timeout_after_commit": TIMEOUT in subset,
                    "max_tool_calls": 20}
        env = registry.environment_factory("refund-world/1")()
        target = registry.target_factory("refund-world/1", "vulnerable")()
        env.reset(asdict(fixture), scenario, actor_id=fixture.customer_id)
        task = TargetTask(task_id="t1", params={"order_id": fixture.order_id,
                                               "customer_id": fixture.customer_id})
        attempt = run_attempt(env, target, task, authorize=lambda a: None,
                              cap=20, cooperative_stop=False)
        evaluator = registry.environment_factory("refund-world/1")()
        try:
            checks = evaluator.evaluate(trace=attempt.trace, state=attempt.state)
        finally:
            evaluator.close()
        if tuple(subset) == (TIMEOUT,):
            captured.update(trace=attempt.trace, state=attempt.state,
                            scenario=scenario, checks=check_verdicts(checks))
        return RunEvidence(status=attempt.status, checks=checks,
                           tool_calls=attempt.tool_calls)

    report = minimize_faults(run=run, faults=[TIMEOUT],
                             invariant=REFUND_INVARIANT)
    assert report.status == "already_minimal"
    regression_path = tmp_path / "ext-regression.json"
    _write_json(regression_path, report.regression)
    evidence_path = tmp_path / "ext-evidence.json"
    _write_json(evidence_path, {
        "world_version": "refund-world/1", "fixture": asdict(fixture),
        "scenario": captured["scenario"], "actor_id": fixture.customer_id,
        "trace": captured["trace"], "state": captured["state"],
        "checks": captured["checks"]})
    return regression_path, evidence_path


def _run_pytest(target, *args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(target), "-q", "-p",
         "no:cacheprovider", *args], cwd=cwd, capture_output=True, text=True,
        timeout=120)


def _import_generated(path):
    name = f"generated_case_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network was contacted")
    monkeypatch.setattr(socket.socket, "connect", blocked)


def _assert_clean_module(source, *, model_calls_asserted=True):
    assert FORBIDDEN.search(source) is None
    assert FORBIDDEN_RUNTIME.search(source) is None
    assert "import ghostlab.replay.export_tests" not in source
    if model_calls_asserted:
        assert source.count('model_calls"] == 0') == 1
    compile(source, "<generated>", "exec")


def test_refund_violation_export_passes(tmp_path):
    out = tmp_path / "test_refund_case.py"
    summary = export_pytest_module(_refund_violation(tmp_path), out)
    assert summary["kind"] == "violation"
    assert summary["world"] == "refund-world/1"
    source = out.read_text(encoding="utf-8")
    assert "load_problem" in source and "problem.replay" in source
    assert "classify(fresh" in source and REFUND_INVARIANT in source
    assert str(tmp_path) not in source
    _assert_clean_module(source)
    case = _refund_violation(tmp_path)
    export_pytest_module(case, tmp_path / "twin_a.py", case_id="twin")
    export_pytest_module(case, tmp_path / "twin_b.py", case_id="twin")
    assert (tmp_path / "twin_a.py").read_bytes() == (tmp_path / "twin_b.py").read_bytes()
    result = _run_pytest(out, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_journey_violation_export_passes(tmp_path):
    out = tmp_path / "test_journey_case.py"
    summary = export_pytest_module(_journey_violation(tmp_path), out,
                                   case_id="journey_privacy")
    assert summary["kind"] == "violation"
    assert summary["world"] == "group-journey/1"
    assert summary["case_id"] == "journey_privacy"
    source = out.read_text(encoding="utf-8")
    assert "participant-c" in source and PRIVACY in source
    _assert_clean_module(source)
    result = _run_pytest(out, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_fixed_export_passes_and_names_the_guard(tmp_path, monkeypatch):
    out = tmp_path / "test_fixed_case.py"
    summary = export_pytest_module(_fixed_case(tmp_path), out,
                                   case_id="refund_fixed")
    assert summary["kind"] == "fixed"
    source = out.read_text(encoding="utf-8")
    assert "EXPECTED_PASSED = True" in source
    assert "REGRESSION" in source and "recurs" in source
    _assert_clean_module(source)
    _no_network(monkeypatch)
    module = _import_generated(out)
    assert module.CASE_ID == "refund_fixed"
    assert module.WORLD_VERSION == "refund-world/1"
    module.test_replay_reproduces_recorded_evidence()
    module.test_fixed_invariant_holds()
    result = _run_pytest(out, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_fixed_export_fails_when_the_violation_recurs(tmp_path, monkeypatch):
    """Negative test: a regressed world fails the fixed-case module loudly."""
    out = tmp_path / "test_fixed_negative.py"
    export_pytest_module(_fixed_case(tmp_path), out)
    module = _import_generated(out)
    real_verify = refund_capsule.verify

    def violated(fixture, state):
        checks = real_verify(fixture, state)
        return {**checks, REFUND_INVARIANT: False}

    monkeypatch.setattr(refund_capsule, "verify", violated)
    with pytest.raises(AssertionError, match="no longer reproduces"):
        module.test_replay_reproduces_recorded_evidence()
    with pytest.raises(AssertionError, match=f"REGRESSION.*{REFUND_INVARIANT}.*recurs"):
        module.test_fixed_invariant_holds()


def test_negative_end_to_end_through_pytest(tmp_path):
    """The same failure direction through a real pytest run."""
    out = tmp_path / "test_fixed_e2e.py"
    export_pytest_module(_fixed_case(tmp_path), out)
    (tmp_path / "conftest.py").write_text(
        "import ghostlab.capsule as refund_capsule\n"
        "_real = refund_capsule.verify\n"
        "def _violated(fixture, state):\n"
        f"    return {{**_real(fixture, state), {REFUND_INVARIANT!r}: False}}\n"
        "refund_capsule.verify = _violated\n",
        encoding="utf-8")
    result = _run_pytest(out, cwd=tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "2 failed" in result.stdout
    assert "REGRESSION" in result.stdout and REFUND_INVARIANT in result.stdout


def test_violation_export_detects_a_changed_world(tmp_path):
    """A violation lock fails (explicitly) when behavior changes underneath."""
    out = tmp_path / "test_lock_breaks.py"
    export_pytest_module(_refund_violation(tmp_path), out)
    (tmp_path / "conftest.py").write_text(
        "import ghostlab.core as core\n"
        "_real = core.run_agent\n"
        "def _repaired_only(world, adapter):\n"
        "    return _real(world, 'repaired')\n"
        "core.run_agent = _repaired_only\n",
        encoding="utf-8")
    result = _run_pytest(out, cwd=tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert ("no longer reproduces" in combined
            or "not what its adapter produces" in combined)


def test_ext_export_registers_only_reviewed_adapters(tmp_path):
    regression_path, evidence_path = _ext_pair(tmp_path)
    out = tmp_path / "test_ext_case.py"
    summary = export_pytest_module(regression_path, out, case_id="ext_timeout",
                                   evidence_path=evidence_path)
    assert summary["kind"] == "ext"
    assert summary["faults"] == [TIMEOUT]
    source = out.read_text(encoding="utf-8")
    assert "replay_recorded" in source
    assert "register_refund_adapters(registry)" in source
    assert "register_reservation_adapters(registry)" in source
    assert len(re.findall(r"register_\w+\(registry\)", source)) == 2
    assert str(tmp_path) not in source
    _assert_clean_module(source)
    result = _run_pytest(out, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_ext_unfinished_search_reports_incomplete(tmp_path):
    from ghostlab.replay.minimize_ext import EXT_REGRESSION_SCHEMA
    from ghostlab.replay.recorded import witness_sha256
    witness = {"failed": [{"name": "x", "passed": False,
                           "evidence_event_ids": [1], "observed": None,
                           "limit": None, "unit": None}]}
    regression_path = tmp_path / "ext-unfinished.json"
    _write_json(regression_path, {
        "schema": EXT_REGRESSION_SCHEMA, "invariant": "x",
        "failure_witness": witness,
        "failure_witness_sha256": witness_sha256(witness),
        "faults": None, "note": "search did not finish"})
    out = tmp_path / "test_ext_unfinished.py"
    summary = export_pytest_module(regression_path, out)
    assert summary["kind"] == "incomplete-unfinished"
    source = out.read_text(encoding="utf-8")
    assert "pytest.skip" in source
    _assert_clean_module(source, model_calls_asserted=False)
    result = _run_pytest(out, "-rs", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout
    assert "unfinished" in result.stdout


def test_ext_without_evidence_reports_incomplete(tmp_path):
    regression_path, _ = _ext_pair(tmp_path)
    out = tmp_path / "test_ext_bare.py"
    summary = export_pytest_module(regression_path, out)
    assert summary["kind"] == "incomplete-no-evidence"
    result = _run_pytest(out, "-rs", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout
    assert "no recorded evidence" in result.stdout
    direct = subprocess.run([sys.executable, str(out)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=120)
    assert direct.returncode == 0, direct.stdout + direct.stderr
    assert "SKIP test_export_is_incomplete" in direct.stdout


def test_generated_modules_run_standalone(tmp_path):
    out = tmp_path / "test_standalone.py"
    export_pytest_module(_refund_violation(tmp_path), out)
    direct = subprocess.run([sys.executable, str(out)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=120)
    assert direct.returncode == 0, direct.stdout + direct.stderr
    assert "PASS test_replay_reproduces_recorded_evidence" in direct.stdout
    assert "PASS test_recorded_failure_still_reproduces" in direct.stdout


def test_case_id_is_sanitized(tmp_path):
    out = tmp_path / "test_sanitized.py"
    summary = export_pytest_module(_refund_violation(tmp_path), out,
                                   case_id="weird id!../9x")
    assert summary["case_id"] == "weird_id____9x"
    assert "CASE_ID = 'weird_id____9x'" in out.read_text(encoding="utf-8")


def _mutated_case(case_path, tmp_path, name, mutate):
    case = json.loads(case_path.read_text(encoding="utf-8"))
    mutate(case)
    sibling = case_path.parent
    clone = tmp_path / "clone"
    clone.mkdir()
    for member in sibling.iterdir():
        (clone / member.name).write_bytes(member.read_bytes())
    mutated = clone / name
    _write_json(mutated, case)
    return mutated


def test_refuses_unknown_schema(tmp_path):
    bad = tmp_path / "bad.json"
    _write_json(bad, {"schema": "ghostlab-diagnostic-regression/99"})
    with pytest.raises(ValueError, match="unsupported schema"):
        export_pytest_module(bad, tmp_path / "out.py")
    assert not (tmp_path / "out.py").exists()


def test_refuses_capsule_sha_mismatch(tmp_path):
    case = _refund_violation(tmp_path)
    mutated = _mutated_case(case, tmp_path, "mutated.json",
                            lambda c: c.update(sha256="0" * 64))
    with pytest.raises(ValueError, match="does not match the case sha256"):
        export_pytest_module(mutated, tmp_path / "out.py")


def test_refuses_missing_sibling_capsule(tmp_path):
    case = _refund_violation(tmp_path)
    mutated = _mutated_case(case, tmp_path, "mutated.json",
                            lambda c: c.update(capsule="gone.json"))
    with pytest.raises(ValueError, match="was not found"):
        export_pytest_module(mutated, tmp_path / "out.py")


def test_refuses_capsule_path_escape(tmp_path):
    case = _refund_violation(tmp_path)
    mutated = _mutated_case(case, tmp_path, "mutated.json",
                            lambda c: c.update(capsule="../escape.json"))
    with pytest.raises(ValueError, match="must be a sibling"):
        export_pytest_module(mutated, tmp_path / "out.py")


def test_refuses_fixed_claim_over_a_violation(tmp_path):
    case = _refund_violation(tmp_path)
    fixed_shape = {
        "schema": "ghostlab-diagnostic-regression/2",
        "capsule": "minimal-capsule.json",
        "sha256": hashlib.sha256(
            (case.parent / "minimal-capsule.json").read_bytes()).hexdigest(),
        "invariant": REFUND_INVARIANT, "expected_passed": True}
    clone = tmp_path / "fixclaim"
    clone.mkdir()
    (clone / "minimal-capsule.json").write_bytes(
        (case.parent / "minimal-capsule.json").read_bytes())
    claimed = clone / "claimed.json"
    _write_json(claimed, fixed_shape)
    with pytest.raises(ValueError, match="expects passed=True"):
        export_pytest_module(claimed, tmp_path / "out.py")


def test_refuses_tampered_witness(tmp_path):
    case = _refund_violation(tmp_path)

    def tamper(loaded):
        loaded["failure_witness"]["ledger"].append(
            {"forged": True, "amount_minor": 1})

    mutated = _mutated_case(case, tmp_path, "mutated.json", tamper)
    with pytest.raises(ValueError, match="does not match"):
        export_pytest_module(mutated, tmp_path / "out.py")


def test_refuses_unknown_invariant(tmp_path):
    case = _refund_violation(tmp_path)
    mutated = _mutated_case(case, tmp_path, "mutated.json",
                            lambda c: c.update(invariant="made-up-check"))
    with pytest.raises(ValueError, match="recorded checks show"):
        export_pytest_module(mutated, tmp_path / "out.py")


def test_refuses_wrong_key_shape(tmp_path):
    case = _refund_violation(tmp_path)

    def flip(loaded):
        loaded["expected_passed"] = True  # keeps the 8 violation keys

    mutated = _mutated_case(case, tmp_path, "mutated.json", flip)
    with pytest.raises(ValueError, match="fixed case keys must be exactly"):
        export_pytest_module(mutated, tmp_path / "out.py")


def test_refuses_overwrite_and_non_py_output(tmp_path):
    case = _refund_violation(tmp_path)
    out = tmp_path / "test_dup.py"
    export_pytest_module(case, out)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        export_pytest_module(case, out)
    with pytest.raises(ValueError, match="must be a .py file"):
        export_pytest_module(case, tmp_path / "out.txt")
    with pytest.raises(ValueError, match="--evidence applies only"):
        export_pytest_module(case, tmp_path / "other.py",
                             evidence_path=tmp_path / "whatever.json")


def test_refuses_unreadable_and_malformed_input(tmp_path):
    with pytest.raises(ValueError, match="cannot read"):
        export_pytest_module(tmp_path / "missing.json", tmp_path / "out.py")
    broken = tmp_path / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        export_pytest_module(broken, tmp_path / "out.py")


def test_refuses_credential_shaped_evidence(tmp_path):
    from ghostlab.replay.export_tests import assert_no_credentials
    with pytest.raises(ValueError, match="credential-shaped key 'api_key'"):
        assert_no_credentials({"trace": [{"request": {"api_key": "sk-x"}}]})
    regression_path, evidence_path = _ext_pair(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["trace"][0]["secret_note"] = "do-not-embed"
    tainted = tmp_path / "tainted-evidence.json"
    _write_json(tainted, evidence)
    with pytest.raises(ValueError, match="credential-shaped key"):
        export_pytest_module(regression_path, tmp_path / "out.py",
                             evidence_path=tainted)


def test_refuses_ext_evidence_mismatching_the_witness(tmp_path):
    regression_path, evidence_path = _ext_pair(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    flipped = next(name for name, hit in evidence["checks"].items()
                   if hit is False)
    evidence["checks"][flipped] = True
    diverged = tmp_path / "diverged-evidence.json"
    _write_json(diverged, evidence)
    with pytest.raises(ValueError, match="do not match the ext failure witness"):
        export_pytest_module(regression_path, tmp_path / "out.py",
                             evidence_path=diverged)


def test_refuses_ext_unknown_world(tmp_path):
    regression_path, evidence_path = _ext_pair(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["world_version"] = "nope-world/9"
    alien = tmp_path / "alien-evidence.json"
    _write_json(alien, evidence)
    with pytest.raises(ValueError, match="unknown world"):
        export_pytest_module(regression_path, tmp_path / "out.py",
                             evidence_path=alien)


def test_refuses_ext_tampered_witness_digest(tmp_path):
    regression_path, evidence_path = _ext_pair(tmp_path)
    regression = json.loads(regression_path.read_text(encoding="utf-8"))
    regression["failure_witness_sha256"] = "1" * 64
    tampered = tmp_path / "tampered-ext.json"
    _write_json(tampered, regression)
    with pytest.raises(ValueError, match="does not match its sha256"):
        export_pytest_module(tampered, tmp_path / "out.py",
                             evidence_path=evidence_path)


def test_cli_exports_and_reports_exit_codes(tmp_path, capsys):
    case = _refund_violation(tmp_path)
    out = tmp_path / "test_cli.py"
    assert main([str(case), "--out", str(out), "--case-id", "cli"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["case_id"] == "cli" and summary["kind"] == "violation"
    with pytest.raises(SystemExit) as refused:
        main([str(tmp_path / "missing.json"), "--out", str(tmp_path / "x.py")])
    assert refused.value.code == 2
    err = capsys.readouterr().err
    assert "export refused" in err and "Traceback" not in err


def test_cli_script_end_to_end(tmp_path):
    case = _refund_violation(tmp_path)
    out = tmp_path / "test_script.py"
    done = subprocess.run(
        [sys.executable, SCRIPT, str(case), "--out", str(out)], cwd=tmp_path,
        capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["kind"] == "violation"
    result = _run_pytest(out, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    bad = subprocess.run(
        [sys.executable, SCRIPT, str(tmp_path / "missing.json"), "--out",
         str(tmp_path / "x.py")], cwd=tmp_path, capture_output=True,
        text=True, timeout=120)
    assert bad.returncode == 2
    assert "export refused" in bad.stderr and "Traceback" not in bad.stderr

def test_refuses_credential_shaped_case_witness(tmp_path):
    case_path = _refund_violation(tmp_path)
    case = json.loads(case_path.read_text(encoding="utf-8"))
    assert type(case["failure_witness"]) is dict
    case["failure_witness"]["api_key"] = "sk-tainted"
    tainted = case_path.parent / "tainted-case.json"
    _write_json(tainted, case)
    with pytest.raises(ValueError, match="credential-shaped key"):
        export_pytest_module(tainted, tmp_path / "out.py")


def test_doc_sources_are_rendered_safe(tmp_path):
    from ghostlab.replay.export_tests import (
        _sanitize_doc_text, render_incomplete_module)
    hostile = "a" + chr(34) * 3 + chr(10) + "__import__" + chr(34) * 3
    clean = _sanitize_doc_text(hostile)
    assert chr(34) not in clean and chr(10) not in clean
    assert clean.strip() == clean and clean
    text = render_incomplete_module(case_id="x", sources=hostile,
                                    reason="unfinished")
    compile(text, "<exported>", "exec")


def test_cli_refusal_is_a_single_line(tmp_path, capsys, monkeypatch):
    import ghostlab.replay.export_tests as exporter
    def boom(*args, **kwargs):
        raise ValueError("first line" + chr(10) + "second line")
    monkeypatch.setattr(exporter, "export_pytest_module", boom)
    with pytest.raises(SystemExit) as refused:
        main([str(tmp_path / "case.json"), "--out",
              str(tmp_path / "x.py")])
    assert refused.value.code == 2
    err = capsys.readouterr().err
    assert err == "export refused: first line second line" + chr(10)
