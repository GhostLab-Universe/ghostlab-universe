"""The regression gate: compares a report with the reviewed expectations.

Any failure blocks promotion, and the command exits non-zero, which fails CI. Numbers
aren't "close enough": a changed run digest, outcome or count is a regression until a
reviewer changes ``evals/expected.json`` in a commit of its own.

The gate also refuses a report that claims live inference, because this suite can't
produce one, and a report whose schema it doesn't know.
"""

import argparse
import json
import sys
from pathlib import Path

from .score import SCHEMA, score_directory

REQUIRED_FIELDS = ("schema", "inference", "live_provider_contacted", "fixture_label",
                   "environment", "aggregate", "cases", "deferred")
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
    for case in report["cases"]:
        problems.extend(f"case missing {name}" for name in CASE_FIELDS if name not in case)
    return problems


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
    for case in report["cases"]:
        for name in case["failed_expectations"]:
            failures.append(f"{case['case_id']}: failed expectation {name}")
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
    report = score_directory(args.directory)
    failures = check(report, json.loads(args.expected.read_text("utf-8")))
    print(json.dumps({"gate": "blocked" if failures else "passed",
                      "aggregate": report["aggregate"], "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
