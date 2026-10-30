"""Run the cases, store the evidence, score it, then apply the gate.

    python -m evals.run --out <dir>                  run and gate; exit 1 blocks promotion
    python -m evals.run --out <dir> --write-expected  rewrite expected.json for review

Must run with ``LANGSMITH_TRACING=false``. Inference is mocked throughout.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .cases import CASES, Recorder
from .gate import check, expected_from
from .score import score_directory

DEFAULT_EXPECTED = Path(__file__).with_name("expected.json")


def run_case(case, out: Path) -> None:
    work = out / "work" / case.case_id
    recorder = Recorder(work, out / "telemetry", case.case_id)
    try:
        case.run(recorder)
        evidence = recorder.result()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    path = out / "evidence" / f"{case.case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=1, sort_keys=True), "utf-8")


def run_all(out: Path, *, only: str | None = None) -> list[str]:
    errors = []
    for case in CASES:
        if only and only not in case.case_id:
            continue
        try:
            run_case(case, out)
        except Exception as error:  # noqa: BLE001 - a crash is a failed case, not a pass
            errors.append(f"{case.case_id}: run raised {type(error).__name__}")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED)
    parser.add_argument("--write-expected", action="store_true")
    parser.add_argument("--only", help="Run matching cases; the gate then reports the rest")
    args = parser.parse_args(argv)
    os.environ.setdefault("LANGSMITH_TRACING", "false")
    args.out.mkdir(parents=True, exist_ok=True)
    errors = run_all(args.out, only=args.only)
    report = score_directory(args.out)
    (args.out / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True), "utf-8")
    if args.write_expected:
        args.expected.write_text(json.dumps(expected_from(report), indent=1, sort_keys=True)
                                 + "\n", "utf-8")
    expected = json.loads(args.expected.read_text("utf-8")) if args.expected.exists() else {}
    failures = [*errors, *check(report, expected)]
    print(json.dumps({"gate": "blocked" if failures else "passed",
                      "aggregate": report["aggregate"], "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
