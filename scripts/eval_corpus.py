"""Changed-agent batch evaluation over a regression corpus (T3 milestone).

Runs one target -- a registered adapter id or an inline probe script --
across saved regression cases (validated exactly like
``scripts/run_regressions.py``) plus, optionally, the 16 dev-defect
scenarios. Prints per-case verdicts and a multidimensional summary; exit 0
only when no case violated and none is undecided, 1 on a failed gate, 2 on
usage errors. No model calls, no network.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostlab.replay.corpus_eval import (evaluate_corpus, format_summary, gate,  # noqa: E402
                                         load_corpus, parse_target)


def _script_target(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path}: cannot load script ({error})") from None
    if isinstance(raw, dict) and set(raw) <= {"id", "actions"} and "actions" in raw:
        return {"kind": "script", **raw}
    if isinstance(raw, list):
        return {"kind": "script", "actions": raw}
    raise ValueError(f"{path}: script is a JSON list of tool/arguments rows, "
                     "or {'id': ..., 'actions': [...]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--target-id", help="registered target adapter id")
    group.add_argument("--script", help="JSON probe script file")
    parser.add_argument("cases", nargs="*", help="case files or directories")
    parser.add_argument("--include-dev", action="store_true",
                        help="add the 16 dev-defect scenarios")
    parser.add_argument("--require-fault", action="append", default=[],
                        help="fault every runnable case must fire (repeatable)")
    parser.add_argument("--cap", type=int, default=None,
                        help="override the per-case tool budget")
    parser.add_argument("--allow", action="append", default=[],
                        help="authorized tool (repeatable; default: all)")
    parser.add_argument("--case", dest="only", action="append", default=[],
                        help="run only this case id (repeatable)")
    parser.add_argument("--out", default=None, help="write the JSON report here")
    args = parser.parse_args(argv)
    if not args.cases and not args.include_dev:
        print("no case files and --include-dev not given")
        return 2
    try:
        spec = {"kind": "registered", "id": args.target_id} \
            if args.target_id else _script_target(args.script)
        target = parse_target(spec)
        corpus = load_corpus(args.cases) if args.cases else []
        report = evaluate_corpus(
            corpus=corpus, target=target, include_dev=args.include_dev,
            required_faults=args.require_fault, cap=args.cap,
            allow=args.allow or None, only=args.only)
    except (ValueError, TypeError, RuntimeError) as error:
        print(f"error: {error}")
        return 2
    print(format_summary(report))
    ok, failed = gate(report)
    if args.out:
        try:
            Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True)
                                      + "\n", encoding="utf-8")
        except OSError as error:
            print(f"error: cannot write {args.out}: {error}")
            return 2
        print(f"report written to {args.out}")
    if not ok:
        print(f"gate FAILED violations={failed['violations']} "
              f"unknowns={failed['unknowns']}")
        return 1
    print("gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
