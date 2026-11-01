"""The regression gate: compares a report with the reviewed expectations.

Any failure blocks promotion, and the command exits non-zero, which fails CI. Numbers
aren't "close enough": a changed run digest, outcome or count is a regression until a
reviewer changes ``evals/expected.json`` in a commit of its own.

The gate also refuses a report that claims live inference, because this suite can't
produce one, and a report whose schema it doesn't know.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .harness import FIXTURE_LABEL
from .identity import evidence_sha256
from .score import RUN_IDENTITY, SCHEMA, score_directory

REPORT_FILE = "report.json"
REQUIRED_FIELDS = ("schema", "inference", "live_provider_contacted", "fixture_label",
                   "environment", "aggregate", "cases", "deferred", "commit", "dirty",
                   "code_sha256", "origin_verified")
CASE_FIELDS = ("case_id", "category", "world", "status", "failed_expectations", "expectations",
               "metrics", "telemetry_records", "local_wall_ms", "fingerprint")


def validate_report(report: dict) -> list[str]:
    if not isinstance(report, dict):
        return ["report is not an object"]
    problems = [f"missing {name}" for name in REQUIRED_FIELDS if name not in report]
    if problems:
        return problems
    if report["schema"] != SCHEMA:
        problems.append("unknown schema")
    if report["inference"] != "mocked" or report["live_provider_contacted"] is not False:
        problems.append("this suite runs on mocked inference only")
    if report["fixture_label"] != FIXTURE_LABEL:
        problems.append("the fixture label isn't this suite's label")
    if report["origin_verified"] is not False:
        problems.append("nothing here can verify a report's origin")
    for case in report["cases"]:
        problems.extend(f"case missing {name}" for name in CASE_FIELDS if name not in case)
    return problems


def _case_problems(case: dict) -> list[str]:
    """A case passes only if every part of it says so."""
    expectations = case["expectations"]
    wrong = sorted(name for name, passed in expectations.items() if not passed)
    if case["status"] != "passed" and not case["failed_expectations"] and not wrong:
        return [f"{case['case_id']}: status {case['status']}"]
    if not expectations and not case["failed_expectations"]:
        return [f"{case['case_id']}: no expectations were measured"]
    return [f"{case['case_id']}: failed expectation {name}"
            for name in sorted({*case["failed_expectations"], *wrong})]


def verified_report(directory: Path) -> tuple[dict, str | None, list[str]]:
    """Rescore the evidence. The stored report must say the same, or it isn't trusted.

    Returns the rescored report, the digest of the stored file's bytes, and any problems.
    """
    report = score_directory(directory)
    identity = Path(directory) / RUN_IDENTITY
    recorded = json.loads(identity.read_text("utf-8")) if identity.exists() else {}
    sealed = recorded.get("evidence_sha256") == evidence_sha256(directory, recorded)
    seal = [] if sealed else [f"{RUN_IDENTITY} doesn't match the evidence files"]
    if recorded and recorded.get("origin_verified") is not False:
        seal.append(f"{RUN_IDENTITY} claims a verified origin, which nothing here can give")
    path = Path(directory) / REPORT_FILE
    if not path.exists():
        return report, None, [*seal, f"no {REPORT_FILE} in the evidence directory"]
    data = path.read_bytes()
    try:
        stored = json.loads(data)
    except ValueError:
        return report, hashlib.sha256(data).hexdigest(), [*seal, f"{REPORT_FILE} isn't JSON"]
    same = isinstance(stored, dict) and (
        {key: value for key, value in stored.items() if key != "environment"}
        == {key: value for key, value in report.items() if key != "environment"})
    problems = [*seal, *([] if same else [f"{REPORT_FILE} doesn't match the evidence"])]
    return report, hashlib.sha256(data).hexdigest(), problems


def _diff(path: str, expected, observed) -> list[str]:
    if isinstance(expected, dict) and isinstance(observed, dict):
        lines = []
        for key in sorted(set(expected) | set(observed)):
            if key not in expected:
                lines.append(f"{path}.{key}: not in expected")
            elif key not in observed:
                lines.append(f"{path}.{key}: missing")
            else:
                lines.extend(_diff(f"{path}.{key}", expected[key], observed[key]))
        return lines
    if expected != observed:
        return [f"{path}: expected {json.dumps(expected)}, observed {json.dumps(observed)}"]
    return []


def check(report: dict, expected: dict) -> list[str]:
    """Reasons to block promotion. Empty means this gate has no objection."""
    failures = validate_report(report)
    if failures:
        return failures
    cases = {case["case_id"]: case for case in report["cases"]}
    passed = sum(case["status"] == "passed" for case in report["cases"])
    if (report["aggregate"].get("cases") or {}) != {"total": len(report["cases"]),
                                                    "passed": passed}:
        failures.append("the aggregate doesn't match the cases")
    for case in report["cases"]:
        failures.extend(_case_problems(case))
    for missing in sorted(set(expected) - set(cases)):
        failures.append(f"{missing}: expected case did not run")
    for extra in sorted(set(cases) - set(expected)):
        failures.append(f"{extra}: not in the reviewed expectations")
    for case_id in sorted(set(expected) & set(cases)):
        failures.extend(_diff(case_id, expected[case_id], cases[case_id]["fingerprint"]))
    return failures


def expected_from(report: dict) -> dict:
    """Fingerprints to review and commit. Never written automatically by CI."""
    return {case["case_id"]: case["fingerprint"] for case in report["cases"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rescore stored evidence and apply the gate")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected", type=Path, default=Path("evals/expected.json"))
    args = parser.parse_args(argv)
    report, _, problems = verified_report(args.directory)
    failures = [*problems, *check(report, json.loads(args.expected.read_text("utf-8")))]
    print(json.dumps({"gate": "blocked" if failures else "passed",
                      "inference": report["inference"], "fixture_label": report["fixture_label"],
                      "aggregate": report["aggregate"], "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
