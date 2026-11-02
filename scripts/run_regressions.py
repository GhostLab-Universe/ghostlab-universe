"""Saved regression evidence runner (P6): replay must reproduce, or CI fails.

Each case file holds recorded evidence plus the expected outcome. The
runner replays every case against a freshly reset environment and fails on
any mismatch: a demonstrated violation that no longer reproduces, a fixed
case that regresses, or evidence the world rejects. Exit 0 when every case
matches, 1 on mismatch, 2 on usage errors. No model calls, no network.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostlab.adapters import register_refund_adapters  # noqa: E402
from ghostlab.adapters.reservation_env import register_reservation_adapters  # noqa: E402
from ghostlab.extensions.registry import AdapterRegistry  # noqa: E402
from ghostlab.replay import replay_recorded  # noqa: E402

CASE_KEYS = frozenset({"case_id", "world_version", "fixture", "scenario", "actor_id",
                       "trace", "state", "checks", "expected_reproduced",
                       "expected_failed"})


def load_case(path: Path) -> dict:
    try:
        case = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path.name}: invalid JSON ({error})") from None
    if type(case) is not dict or set(case) != CASE_KEYS:
        raise ValueError(f"{path.name}: keys must be exactly {sorted(CASE_KEYS)}")
    if type(case["case_id"]) is not str or not case["case_id"]:
        raise ValueError(f"{path.name}: invalid case_id")
    if type(case["trace"]) is not list or not isinstance(case["state"], dict):
        raise ValueError(f"{path.name}: invalid trace or state")
    if (type(case["checks"]) is not dict
            or any(type(hit) is not bool for hit in case["checks"].values())):
        raise ValueError(f"{path.name}: checks map names to booleans")
    if type(case["expected_reproduced"]) is not bool:
        raise ValueError(f"{path.name}: expected_reproduced must be boolean")
    if (not isinstance(case["expected_failed"], list)
            or any(type(name) is not str for name in case["expected_failed"])):
        raise ValueError(f"{path.name}: expected_failed must be a string list")
    return case


def run_case(registry: AdapterRegistry, case: dict) -> tuple:
    result = replay_recorded(
        registry=registry, world_version=case["world_version"],
        fixture=case["fixture"], scenario=case["scenario"],
        actor_id=case["actor_id"], recorded_trace=case["trace"],
        recorded_state=case["state"], recorded_checks=case["checks"]).to_dict()
    problems = []
    if result["reproduced"] != case["expected_reproduced"]:
        problems.append(f"reproduced={result['reproduced']} "
                        f"(expected {case['expected_reproduced']})")
    if sorted(result["failed"]) != sorted(case["expected_failed"]):
        problems.append(f"failed={result['failed']} "
                        f"(expected {case['expected_failed']})")
    return result["reproduced"], problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="+", help="case files or directories")
    args = parser.parse_args(argv)
    paths = []
    for raw in args.cases:
        path = Path(raw)
        if not path.exists():
            print(f"missing: {raw}")
            return 2
        paths.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
    if not paths:
        print("no case files")
        return 2
    registry = AdapterRegistry()
    register_refund_adapters(registry)
    register_reservation_adapters(registry)
    failed = 0
    for path in paths:
        try:
            case = load_case(path)
            reproduced, problems = run_case(registry, case)
        except (ValueError, LookupError, TypeError) as error:
            print(f"FAIL {path.name}: {error}")
            failed += 1
            continue
        if problems:
            print(f"FAIL {case['case_id']}: " + "; ".join(problems))
            failed += 1
        else:
            print(f"PASS {case['case_id']} (reproduced={reproduced})")
    print(f"{len(paths) - failed}/{len(paths)} regression cases pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
