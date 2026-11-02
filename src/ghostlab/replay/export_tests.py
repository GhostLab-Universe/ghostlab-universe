"""Generate runnable pytest modules from saved regression cases.

Reads a ``ghostlab-diagnostic-regression/2`` case (plus the sibling capsule
file it names) or a ``ghostlab-diagnostic-regression-ext/1`` dict (plus
paired recorded evidence) and emits a standalone pytest module.

The generated module replays the case and locks the recorded expectation:

* a violation case (``expected_passed`` False, or any ext dict) passes while
  the SAME violation reproduces deterministically -- same invariant, same
  failure witness -- and fails meaningfully when the behavior changes
  (fixed, different failure, or evidence that no longer replays);
* a fixed case (``expected_passed`` True) passes while the invariant holds
  and fails when the defined violation recurs.

Semantics mirror :mod:`scripts.run_regressions` (reused here by import for
the extension-evidence pre-flight; that file is never edited): replay must
reproduce the recorded evidence or the test fails. Unknown worlds fail
closed, unfinished searches and missing evidence become explicit
``pytest.skip`` reports -- never a silent pass -- and generation refuses to
emit a test that would fail on the current tree.

Generated modules make zero model calls and embed no credentials: embedded
evidence is scanned for credential-shaped keys before it is written, and
only reviewed checked-in adapters can run (capsule replay runs the named
checked-in adapter and refuses unknown ones; extension replay uses a fresh
registry with only the two reviewed registrations).

No new contract versions are introduced: inputs are the frozen ``/2`` and
``ext/1`` regression dicts, and paired evidence is plain replay data shaped
exactly like the ``run_regressions`` case fields (without a schema tag).
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

from ghostlab import capsule as refund_capsule
from ghostlab.diagnostics.minimize import canonical, classify, load_problem
from ghostlab.replay.minimize_ext import EXT_REGRESSION_SCHEMA
from ghostlab.replay.recorded import normalize_check, witness_sha256

REGRESSION_V2 = "ghostlab-diagnostic-regression/2"
EVIDENCE_KEYS = frozenset({"world_version", "fixture", "scenario", "actor_id",
                           "trace", "state", "checks"})
EMBED_LIMIT_BYTES = 512_000

CREDENTIAL_KEY_PARTS = ("api_key", "apikey", "secret", "passwd", "password",
                        "access_token", "auth_token", "session_token",
                        "refresh_token", "bearer", "authorization",
                        "private_key", "client_secret", "credentials")

V2_FALSE_KEYS = frozenset({"schema", "capsule", "sha256", "invariant",
                           "expected_passed", "failure_witness_sha256",
                           "failure_witness", "recipient_witnesses"})
V2_TRUE_KEYS = frozenset({"schema", "capsule", "sha256", "invariant",
                          "expected_passed"})
EXT_KEYS = frozenset({"schema", "invariant", "failure_witness",
                      "failure_witness_sha256", "faults"})
EXT_UNFINISHED_KEYS = frozenset({"schema", "invariant", "failure_witness",
                                 "failure_witness_sha256", "faults", "note"})

_run_case = None


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "run_regressions.py").is_file():
            return parent
    raise ValueError("export needs a repository checkout: "
                     "scripts/run_regressions.py was not found")


def _run_regression_case(registry, case):
    """Compare one P6 case via scripts/run_regressions.py (imported, not edited)."""
    global _run_case
    if _run_case is None:
        path = _repo_root() / "scripts" / "run_regressions.py"
        try:
            spec = importlib.util.spec_from_file_location("run_regressions", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _run_case = module.run_case
        except (ImportError, AttributeError) as error:
            raise ValueError("cannot reuse scripts/run_regressions.py: "
                             f"{error}") from None
    return _run_case(registry, case)


def reviewed_registry():
    """A fresh registry with ONLY the reviewed checked-in adapters."""
    from ghostlab.adapters import register_refund_adapters
    from ghostlab.adapters.reservation_env import register_reservation_adapters
    from ghostlab.extensions.registry import AdapterRegistry
    registry = AdapterRegistry()
    register_refund_adapters(registry)
    register_reservation_adapters(registry)
    return registry


def assert_no_credentials(value, where="embedded evidence"):
    """Refuse credential-shaped keys anywhere in data bound for a test module."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(key, str) and any(
                        part in key.lower() for part in CREDENTIAL_KEY_PARTS):
                    raise ValueError(
                        f"refusing to embed {where}: credential-shaped key {key!r}")
                stack.append(item)
        elif isinstance(node, list):
            stack.extend(node)
    return value


def _json_literal(value):
    """Deterministic JSON text of a value, as an embeddable Python literal."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return json.dumps(text)


def _text_literal(text):
    """Verbatim text (e.g. capsule bytes) as an embeddable Python literal."""
    return json.dumps(text, ensure_ascii=True)


def _check_sha256(value, label):
    if (type(value) is not str or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)):
        raise ValueError(f"invalid {label}: must be a 64-character hex digest")


def _sanitize_case_id(raw):
    text = "".join(c if c.isascii() and (c.isalnum() or c == "_") else "_"
                  for c in str(raw))
    text = text.strip("_") or "case"
    if text[0].isdigit():
        text = "case_" + text
    return text[:80]


def _load_json(path: Path, label):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read {label} {path.name}: {error.strerror}") from None
    if len(text.encode()) > refund_capsule.MAX_CAPSULE_BYTES:
        raise ValueError(f"{label} {path.name} is too large")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} {path.name}: invalid JSON ({error})") from None
    if type(data) is not dict:
        raise ValueError(f"{label} {path.name}: top level must be an object")
    return data


def validate_v2_case(case, case_path: Path):
    """Validate a /2 case against its sibling capsule and the current tree.

    Pre-flights the exact replay the generated test will run and refuses to
    export a test that would fail here: an export is a passing test plus its
    failure direction, never a red test by accident.
    """
    if case.get("schema") != REGRESSION_V2:
        raise ValueError("not a ghostlab-diagnostic-regression/2 case")
    expected = case.get("expected_passed")
    if expected is False:
        if set(case) != V2_FALSE_KEYS:
            raise ValueError(f"violation case keys must be exactly {sorted(V2_FALSE_KEYS)}")
    elif expected is True:
        if set(case) != V2_TRUE_KEYS:
            raise ValueError(f"fixed case keys must be exactly {sorted(V2_TRUE_KEYS)}")
    else:
        raise ValueError("expected_passed must be an exact boolean")
    name = case.get("capsule")
    if (type(name) is not str or not name or name != Path(name).name
            or not name.endswith(".json")):
        raise ValueError("capsule must be a sibling JSON file name, not a path")
    invariant = case.get("invariant")
    if type(invariant) is not str or not invariant:
        raise ValueError("invalid invariant")
    _check_sha256(case.get("sha256"), "capsule sha256")
    capsule_path = case_path.parent / name
    if not capsule_path.is_file():
        raise ValueError(f"sibling capsule {name} was not found next to {case_path.name}")
    blob = capsule_path.read_bytes()
    if len(blob) > refund_capsule.MAX_CAPSULE_BYTES:
        raise ValueError(f"sibling capsule {name} is too large")
    if len(blob) > EMBED_LIMIT_BYTES:
        raise ValueError(f"sibling capsule {name} is too large to embed "
                         f"({len(blob)} > {EMBED_LIMIT_BYTES} bytes)")
    digest = hashlib.sha256(blob).hexdigest()
    if digest != case["sha256"]:
        raise ValueError(f"sibling capsule {name} does not match the case sha256")
    envelope = json.loads(blob.decode("utf-8"))
    assert_no_credentials(envelope, where=f"capsule {name}")
    problem = load_problem(capsule_path)
    recorded_checks = problem.original_run.get("checks", {})
    if recorded_checks.get(invariant) is not expected:
        raise ValueError(f"recorded checks show {invariant}="
                         f"{recorded_checks.get(invariant)!r}, but the case "
                         f"expects passed={expected}")
    replayed = problem.replay(capsule_path)
    if replayed["reproduced"] is not True:
        raise ValueError(f"capsule {name} does not reproduce on this tree "
                         f"(matches={replayed['matches']}): refusing to export "
                         "a test that would fail")
    recipients: list = []
    witness = None
    witness_sha = None
    if expected is False:
        recipients = case["recipient_witnesses"]
        if (not isinstance(recipients, list)
                or any(type(entry) is not str for entry in recipients)):
            raise ValueError("recipient_witnesses must be a string list")
        witness = case["failure_witness"]
        witness_sha = case["failure_witness_sha256"]
        _check_sha256(witness_sha, "failure witness sha256")
        if hashlib.sha256(canonical(witness)).hexdigest() != witness_sha:
            raise ValueError("failure_witness does not match failure_witness_sha256")
        fresh = problem.run(problem.faults)
        verdict = classify(fresh, invariant, set(recipients), problem=problem,
                           expected_witness=witness)
        if verdict != "reproduced":
            raise ValueError(f"the recorded violation of {invariant} does not "
                             f"reproduce on this tree (verdict={verdict}): "
                             "refusing to export a test that would fail")
    else:
        fresh = problem.run(problem.faults)
        if fresh["checks"].get(invariant) is not True:
            raise ValueError(f"fresh run shows {invariant} failing, but the case "
                             "is a fixed case (expected_passed=True)")
    return {"world": problem.world, "invariant": invariant,
            "expected_passed": expected, "recipients": sorted(recipients),
            "witness": witness, "witness_sha": witness_sha,
            "capsule_sha": digest, "capsule_text": blob.decode("utf-8"),
            "recorded_status": problem.original_run.get("status")}


def validate_ext_regression(data):
    """Validate an ext regression dict; unfinished searches stay explicit."""
    if data.get("schema") != EXT_REGRESSION_SCHEMA:
        raise ValueError(f"not a {EXT_REGRESSION_SCHEMA} dict")
    if set(data) not in (EXT_KEYS, EXT_UNFINISHED_KEYS):
        raise ValueError("ext regression keys must be the minimize_ext "
                         "regression dict")
    invariant = data.get("invariant")
    if type(invariant) is not str or not invariant:
        raise ValueError("invalid ext invariant")
    witness = data.get("failure_witness")
    if type(witness) is not dict or set(witness) != {"failed"}:
        raise ValueError("invalid ext failure witness")
    if type(witness["failed"]) is not list or not witness["failed"]:
        raise ValueError("invalid ext failure witness")
    for row in witness["failed"]:
        if normalize_check(row)["passed"] is not False:
            raise ValueError("ext failure witness names only failed checks")
    _check_sha256(data.get("failure_witness_sha256"), "ext witness sha256")
    if witness_sha256(witness) != data["failure_witness_sha256"]:
        raise ValueError("ext failure_witness does not match its sha256")
    faults = data.get("faults")
    if faults is None:
        return {"invariant": invariant, "witness": witness,
                "witness_sha": data["failure_witness_sha256"], "faults": None,
                "note": data.get("note", "search did not finish")}
    if (not isinstance(faults, list)
            or any(type(entry) is not str or not entry for entry in faults)):
        raise ValueError("ext faults must be a string list or null")
    return {"invariant": invariant, "witness": witness,
            "witness_sha": data["failure_witness_sha256"],
            "faults": list(faults), "note": None}


def validate_evidence(evidence, regression):
    """Validate paired recorded evidence by replaying it through run_regressions.

    The evidence must reproduce on the current tree with the reviewed
    adapters, fail exactly the witnessed invariants, and fail the selected
    invariant -- otherwise no test is emitted.
    """
    if set(evidence) != EVIDENCE_KEYS:
        raise ValueError(f"evidence keys must be exactly {sorted(EVIDENCE_KEYS)}")
    world = evidence.get("world_version")
    if type(world) is not str or not world:
        raise ValueError("invalid evidence world_version")
    if type(evidence.get("fixture")) is not dict:
        raise ValueError("invalid evidence fixture")
    if type(evidence.get("scenario")) is not dict:
        raise ValueError("invalid evidence scenario")
    if type(evidence.get("actor_id")) is not str or not evidence["actor_id"]:
        raise ValueError("invalid evidence actor_id")
    if type(evidence.get("trace")) is not list or len(evidence["trace"]) > 1000:
        raise ValueError("invalid evidence trace")
    if type(evidence.get("state")) is not dict:
        raise ValueError("invalid evidence state")
    checks = evidence.get("checks")
    if (type(checks) is not dict or not checks
            or any(type(hit) is not bool for hit in checks.values())):
        raise ValueError("evidence checks map names to booleans")
    assert_no_credentials(evidence, where="ext evidence")
    failed = sorted(name for name, hit in checks.items() if hit is False)
    witnessed = sorted(row["name"] for row in regression["witness"]["failed"])
    if failed != witnessed:
        raise ValueError("evidence failures do not match the ext failure "
                         f"witness ({failed} != {witnessed})")
    if checks.get(regression["invariant"]) is not False:
        raise ValueError(f"evidence does not fail {regression['invariant']!r}")
    from ghostlab.replay.recorded import replay_recorded
    registry = reviewed_registry()
    case = {"case_id": "preflight", "world_version": world,
            "fixture": evidence["fixture"], "scenario": evidence["scenario"],
            "actor_id": evidence["actor_id"], "trace": evidence["trace"],
            "state": evidence["state"], "checks": dict(checks),
            "expected_reproduced": True, "expected_failed": failed}
    try:
        _, problems = _run_regression_case(registry, case)
    except LookupError as error:
        raise ValueError(f"unknown world {world!r}: only reviewed checked-in "
                         f"adapters replay here ({error})") from None
    if problems:
        raise ValueError("ext evidence does not reproduce on this tree "
                         f"({'; '.join(problems)}): refusing to export a test "
                         "that would fail")
    seen = replay_recorded(
        registry=reviewed_registry(), world_version=world,
        fixture=evidence["fixture"], scenario=evidence["scenario"],
        actor_id=evidence["actor_id"], recorded_trace=evidence["trace"],
        recorded_state=evidence["state"],
        recorded_checks=dict(checks)).to_dict()
    if seen["witness"] != regression["witness"]:
        raise ValueError("replayed ext witness differs from the regression "
                         "witness: refusing to export")
    return {"world": world, "failed": failed}


_MAIN_RUNNER = '''

if __name__ == "__main__":
    import traceback
    failures = 0
    tests = sorted((name, func) for name, func in globals().items()
                   if name.startswith("test_") and callable(func))
    for name, func in tests:
        try:
            func()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            if type(error).__name__.startswith("Skip"):
                print(f"SKIP {name}: {error}")
            else:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
        else:
            print(f"PASS {name}")
    raise SystemExit(1 if failures else 0)
'''

_V2_TEMPLATE = '''"""Regression test for __SOURCE__.

Exported from __SOURCE__ (case __CASE_ID__); do not hand-edit, regenerate with
``python scripts/export_regression_tests.py __SOURCE__ --out <this file>``.

__EXPECTATION_DOC__
Replay runs the capsule's named checked-in adapter and refuses unknown ones;
no model is called and no credential is embedded. Needs only the ghostlab
package (standard library path) plus pytest to run.
"""

import hashlib
import json
import tempfile
from pathlib import Path

from ghostlab.diagnostics.minimize import (canonical, classify, failure_witness,
                                            load_problem)

CASE_ID = __CASE_ID__
WORLD_VERSION = __WORLD__
INVARIANT = __INVARIANT__
EXPECTED_PASSED = __EXPECTED_PASSED__
RECIPIENT_WITNESSES = __RECIPIENTS__
FAILURE_WITNESS = __WITNESS_DEF__
FAILURE_WITNESS_SHA256 = __WITNESS_SHA__
CAPSULE_SHA256 = __CAPSULE_SHA__
RECORDED_STATUS = __RECORDED_STATUS__
CAPSULE_JSON = __CAPSULE_JSON__


def _load_problem(tmpdir):
    """Write the embedded capsule aside and open it through the frozen loader."""
    path = Path(tmpdir) / "capsule.json"
    path.write_bytes(CAPSULE_JSON.encode("utf-8"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == CAPSULE_SHA256, (
        f"embedded capsule changed: {digest} != {CAPSULE_SHA256}")
    return load_problem(path), path


def test_replay_reproduces_recorded_evidence():
    """Recorded trace, final state and checks replay exactly on a fresh world."""
    with tempfile.TemporaryDirectory(prefix="ghostlab-regression-") as tmpdir:
        problem, path = _load_problem(tmpdir)
        result = problem.replay(path)
        assert result["model_calls"] == 0
        assert result["reproduced"] is True, (
            "recorded evidence no longer reproduces: "
            f"matches={result['matches']}, checks={result['checks']}")


__SECOND_TEST__
'''

_V2_VIOLATION_TEST = '''

def test_recorded_failure_still_reproduces():
    """The minimized violation recurs with the identical failure witness."""
    with tempfile.TemporaryDirectory(prefix="ghostlab-regression-") as tmpdir:
        problem, _ = _load_problem(tmpdir)
        fresh = problem.run(problem.faults)
        seen = failure_witness(problem, fresh, INVARIANT)
        seen_sha = hashlib.sha256(canonical(seen)).hexdigest()
        assert seen_sha == FAILURE_WITNESS_SHA256, (
            f"same invariant {INVARIANT} failed with DIFFERENT evidence: "
            f"witness sha {seen_sha} != recorded {FAILURE_WITNESS_SHA256}")
        verdict = classify(fresh, INVARIANT, set(RECIPIENT_WITNESSES),
                           problem=problem, expected_witness=FAILURE_WITNESS)
        assert verdict == "reproduced", (
            f"recorded violation of {INVARIANT} no longer reproduces "
            f"(verdict={verdict}, status={fresh['status']}, "
            f"checks={fresh['checks']}); update or drop this case, "
            "do not silence it")
'''

_V2_FIXED_TEST = '''

def test_fixed_invariant_holds():
    """The fixed invariant holds: fail loudly if the violation recurs."""
    with tempfile.TemporaryDirectory(prefix="ghostlab-regression-") as tmpdir:
        problem, path = _load_problem(tmpdir)
        result = problem.replay(path)
        assert result["checks"][INVARIANT] is True, (
            f"REGRESSION: violation of {INVARIANT} recurs: "
            f"replayed checks={result['checks']}")
'''

_EXT_TEMPLATE = '''"""Regression test for __SOURCES__.

Exported from __SOURCES__ (case __CASE_ID__); do not hand-edit, regenerate with
``python scripts/export_regression_tests.py <regression.json> --out <this file>``
plus ``--evidence <evidence.json>``.

The recorded violation of __INVARIANT__ must keep reproducing with the
identical failure witness. Replay uses a fresh registry with ONLY the reviewed
checked-in adapters; unknown worlds fail closed. No model is called and no
credential is embedded.
"""

import json

from ghostlab.adapters import register_refund_adapters
from ghostlab.adapters.reservation_env import register_reservation_adapters
from ghostlab.extensions.registry import AdapterRegistry
from ghostlab.replay.recorded import replay_recorded

CASE_ID = __CASE_ID__
WORLD_VERSION = __WORLD__
INVARIANT = __INVARIANT__
EXPECTED_FAILED = __EXPECTED_FAILED__
FAILURE_WITNESS = json.loads(__WITNESS_JSON__)
FAILURE_WITNESS_SHA256 = __WITNESS_SHA__
FAULTS = __FAULTS__
EVIDENCE = json.loads(__EVIDENCE_JSON__)


def _replay():
    """Replay the embedded evidence with only the reviewed adapters."""
    registry = AdapterRegistry()
    register_refund_adapters(registry)
    register_reservation_adapters(registry)
    return replay_recorded(
        registry=registry, world_version=EVIDENCE["world_version"],
        fixture=EVIDENCE["fixture"], scenario=EVIDENCE["scenario"],
        actor_id=EVIDENCE["actor_id"], recorded_trace=EVIDENCE["trace"],
        recorded_state=EVIDENCE["state"],
        recorded_checks=dict(EVIDENCE["checks"])).to_dict()


def test_replay_reproduces_recorded_evidence():
    """Recorded trace, final state and checks replay exactly on a fresh world."""
    result = _replay()
    assert result["model_calls"] == 0
    assert result["reproduced"] is True, (
        "recorded evidence no longer reproduces: "
        f"matches={result['matches']}, divergence={result['divergence']}")


def test_recorded_failure_still_reproduces():
    """The minimized violation recurs with the identical failure witness."""
    result = _replay()
    assert result["checks"][INVARIANT] is False, (
        f"recorded violation of {INVARIANT} no longer reproduces: "
        f"checks={result['checks']}; update or drop this case, "
        "do not silence it")
    assert sorted(result["failed"]) == EXPECTED_FAILED, (
        f"failing set changed: {sorted(result['failed'])} != {EXPECTED_FAILED}")
    assert result["witness"] == FAILURE_WITNESS, (
        "same invariant failed with DIFFERENT evidence: "
        f"witness sha {result['witness_sha256']} != recorded "
        f"{FAILURE_WITNESS_SHA256}")
'''

_INCOMPLETE_TEMPLATE = '''"""Incomplete regression export of __SOURCES__ (case __CASE_ID__).

The minimization did not finish, so there is no confirmed reproducer to lock:
this module reports the export as unknown instead of faking a test.
"""

import pytest

CASE_ID = __CASE_ID__
REASON = __REASON__


def test_export_is_incomplete():
    """The export is unknown: skip explicitly, never pass silently."""
    pytest.skip(f"incomplete regression export {CASE_ID}: {REASON}")
'''


def render_v2_module(validated, *, case_id, source):
    """Render the standalone pytest module for a validated /2 case."""
    if validated["expected_passed"]:
        expectation = (f"The fixed invariant {validated['invariant']} must hold: "
                       "the test fails if the defined violation recurs.")
        second, witness_def, witness_sha = _V2_FIXED_TEST, "None", "None"
    else:
        expectation = (f"The recorded violation of {validated['invariant']} must "
                       "keep reproducing with the identical failure witness.")
        second = _V2_VIOLATION_TEST
        witness_def = f"json.loads({_json_literal(validated['witness'])})"
        witness_sha = repr(validated["witness_sha"])
    text = _V2_TEMPLATE
    for token, value in (
            ("__SOURCE__", source),
            ("__CASE_ID__", repr(case_id)),
            ("__WORLD__", repr(validated["world"])),
            ("__INVARIANT__", repr(validated["invariant"])),
            ("__EXPECTED_PASSED__", repr(validated["expected_passed"])),
            ("__RECIPIENTS__", repr(validated["recipients"])),
            ("__WITNESS_DEF__", witness_def),
            ("__WITNESS_SHA__", witness_sha),
            ("__CAPSULE_SHA__", repr(validated["capsule_sha"])),
            ("__RECORDED_STATUS__", repr(validated["recorded_status"])),
            ("__CAPSULE_JSON__", _text_literal(validated["capsule_text"])),
            ("__SECOND_TEST__", second.strip("\n")),
            ("__EXPECTATION_DOC__", expectation)):
        text = text.replace(token, value)
    return text + _MAIN_RUNNER


def render_ext_module(*, case_id, sources, world, invariant, failed, witness,
                      witness_sha, faults, evidence):
    """Render the standalone pytest module for validated ext evidence."""
    text = _EXT_TEMPLATE
    for token, value in (
            ("__SOURCES__", sources),
            ("__CASE_ID__", repr(case_id)),
            ("__WORLD__", repr(world)),
            ("__INVARIANT__", repr(invariant)),
            ("__EXPECTED_FAILED__", repr(sorted(failed))),
            ("__WITNESS_JSON__", _json_literal(witness)),
            ("__WITNESS_SHA__", repr(witness_sha)),
            ("__FAULTS__", repr(list(faults))),
            ("__EVIDENCE_JSON__", _json_literal(evidence))):
        text = text.replace(token, value)
    return text + _MAIN_RUNNER


def render_incomplete_module(*, case_id, sources, reason):
    """Render the explicit unknown-report for an unfinished or bare export."""
    text = _INCOMPLETE_TEMPLATE
    for token, value in (("__SOURCES__", sources),
                         ("__CASE_ID__", repr(case_id)),
                         ("__REASON__", repr(reason))):
        text = text.replace(token, value)
    return text + _MAIN_RUNNER


def export_pytest_module(case_path, out_path, *, case_id=None, evidence_path=None):
    """Validate a regression case and write its standalone pytest module.

    Returns a summary dict. Refuses (ValueError, no file written) when the
    case is malformed, its evidence does not reproduce on this tree, or the
    output file already exists.
    """
    case_path, out_path = Path(case_path), Path(out_path)
    if out_path.suffix != ".py":
        raise ValueError(f"output {out_path.name} must be a .py file")
    if out_path.exists():
        raise ValueError(f"refusing to overwrite existing {out_path.name}")
    case = _load_json(case_path, "regression case")
    schema = case.get("schema")
    if schema == REGRESSION_V2:
        if evidence_path is not None:
            raise ValueError("--evidence applies only to ext regression dicts")
        validated = validate_v2_case(case, case_path)
        cid = _sanitize_case_id(case_id or case_path.stem)
        text = render_v2_module(validated, case_id=cid, source=case_path.name)
        summary = {"case_id": cid, "kind": ("fixed" if validated["expected_passed"]
                                            else "violation"),
                   "world": validated["world"], "invariant": validated["invariant"],
                   "expected_passed": validated["expected_passed"],
                   "capsule_sha256": validated["capsule_sha"]}
    elif schema == EXT_REGRESSION_SCHEMA:
        regression = validate_ext_regression(case)
        cid = _sanitize_case_id(case_id or case_path.stem)
        sources = case_path.name
        if regression["faults"] is None:
            reason = (f"minimization unfinished ({regression['note']}); rerun "
                      "the search, then re-export")
            text = render_incomplete_module(case_id=cid, sources=sources,
                                            reason=reason)
            summary = {"case_id": cid, "kind": "incomplete-unfinished",
                       "invariant": regression["invariant"]}
        elif evidence_path is None:
            reason = ("ext regression dict carries no recorded evidence; "
                      "re-export with --evidence <evidence.json>")
            text = render_incomplete_module(case_id=cid, sources=sources,
                                            reason=reason)
            summary = {"case_id": cid, "kind": "incomplete-no-evidence",
                       "invariant": regression["invariant"]}
        else:
            evidence = _load_json(Path(evidence_path), "evidence")
            info = validate_evidence(evidence, regression)
            sources = f"{case_path.name} + {Path(evidence_path).name}"
            text = render_ext_module(
                case_id=cid, sources=sources, world=info["world"],
                invariant=regression["invariant"], failed=info["failed"],
                witness=regression["witness"],
                witness_sha=regression["witness_sha"],
                faults=regression["faults"], evidence=evidence)
            summary = {"case_id": cid, "kind": "ext",
                       "world": info["world"],
                       "invariant": regression["invariant"],
                       "faults": regression["faults"]}
    else:
        raise ValueError(f"unsupported schema {schema!r}: want {REGRESSION_V2} "
                         f"or {EXT_REGRESSION_SCHEMA}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out_path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        raise ValueError(f"refusing to overwrite existing {out_path.name}") from None
    return {"out": str(out_path), **summary}


def main(argv=None) -> int:
    """Export CLI: exit 0 with a summary, 2 on any refused input."""
    parser = argparse.ArgumentParser(
        description="Export a runnable pytest module from a saved regression "
                    "case; no model calls")
    parser.add_argument("case", type=Path,
                        help="regression-case.json (/2) or ext regression dict")
    parser.add_argument("--out", type=Path, required=True,
                        help="new .py file to write (never overwritten)")
    parser.add_argument("--case-id", default=None,
                        help="test case id (default: input file stem)")
    parser.add_argument("--evidence", type=Path, default=None,
                        help="recorded evidence JSON paired with an ext dict")
    args = parser.parse_args(argv)
    try:
        summary = export_pytest_module(args.case, args.out,
                                       case_id=args.case_id,
                                       evidence_path=args.evidence)
    except (ValueError, TypeError, KeyError, OSError, RecursionError) as error:
        parser.exit(2, f"export refused: {error}\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0
