"""python -m ghostlab.diagnostics CAPSULE --invariant NAME --out NEW_DIRECTORY"""

import argparse
import json
from pathlib import Path

from .minimize import minimize_capsule


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline fault-subset minimization; no model calls")
    parser.add_argument("capsule", type=Path)
    parser.add_argument("--invariant", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-runs", type=int, default=16)
    parser.add_argument("--deadline-ms", type=int, default=5000)
    args = parser.parse_args(argv)
    try:
        report = minimize_capsule(args.capsule, args.out, invariant=args.invariant,
                                  max_runs=args.max_runs, deadline_ms=args.deadline_ms)
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        parser.exit(2, "Input was refused or output could not be written; no model was called.\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["capsule_reproduced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
