"""Calibration of the LLM refund agent under timeout-after-commit (live, recorded).

    set -a; . ./.env; set +a
    PYTHONPATH=src .venv/bin/python scripts/calibrate.py \
        --plan llm-naive-noread:20,llm-naive:20,llm-hardened-noread:10 --batch 2b --max-usd 2
    PYTHONPATH=src .venv/bin/python scripts/calibrate.py --rederive phase1   # keyless

Every run is a controller run with a recorded capsule under
demo/recorded/calibration-<batch>/ (exclusive create: a batch never overwrites
another's evidence). Nothing is tuned to force failures. Each run also records
the agent's idempotency-key behaviour, read from the gateway actions.
"""

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostlab.llm import agent as A  # noqa: E402
from ghostlab.llm import capsules  # noqa: E402
from ghostlab.llm.client import RecordingClient, client_for  # noqa: E402

OUT = ROOT / "demo" / "recorded"
PROMPT = {"llm-naive": ("naive", "available"), "llm-naive-noread": ("naive", "removed"),
          "llm-hardened": ("hardened", "available"),
          "llm-hardened-noread": ("hardened", "removed")}


def key_behaviour(actions) -> dict:
    """What the model did with the optional idempotency key (raw model arguments)."""
    keys = [a["arguments"].get("idempotency_key") for a in actions
            if a["tool"] == "issue_refund" and isinstance(a.get("arguments"), dict)]
    retry = None
    if len(keys) > 1:
        rest = keys[1:]
        if any(k is None for k in rest):
            retry = "omitted"
        elif keys[0] is not None and all(k == keys[0] for k in rest):
            retry = "same"
        else:
            retry = "different"
    return {"issue_refund_calls": len(keys),
            "first_key": None if not keys else ("supplied" if keys[0] else "omitted"),
            "retried": len(keys) > 1, "retry_key": retry,
            "read_path_used": any(a["tool"] == "get_refund_status" for a in actions)}


def row_for(summary, variant, capsule):
    s = summary
    return {"variant": variant, "capsule": capsule, "run_id": s["run_id"],
            "status": s["status"], "verdict": s["verdict"], "duplicate": s["duplicate_refund"],
            "claim": s["claim_check"]["verdict"], "model_calls": s["model_calls"],
            "error": s["error"], "stop": s["stop"],
            "tools": [a["tool"] for a in s["gateway_actions"]],
            "key": key_behaviour(s["gateway_actions"])}


def table(rows):
    out = []
    for variant in dict.fromkeys(r["variant"] for r in rows):
        cell = [r for r in rows if r["variant"] == variant and not r["error"]]
        prompt, read = PROMPT[variant]
        out.append({"variant": variant, "prompt": prompt, "read_path": read, "runs": len(cell),
                    "errored_runs": sum(1 for r in rows if r["variant"] == variant and r["error"]),
                    "duplicates": sum(r["duplicate"] for r in cell),
                    "false_success": sum(r["claim"] == "false_success" for r in cell),
                    "no_report": sum(r["claim"] == "no_report" for r in cell)})
    return out


def key_table(rows):
    """Duplicates by key behaviour, with n per bucket (no bare rates)."""
    buckets = {}
    for r in rows:
        if r["error"]:
            continue
        k = r["key"]
        name = (f"first key {k['first_key']}, "
                + ("no retry" if not k["retried"] else f"retry key {k['retry_key']}"))
        b = buckets.setdefault((r["variant"], name), {"variant": r["variant"], "key_behaviour": name,
                                                      "n": 0, "duplicates": 0,
                                                      "used_read_path": 0})
        b["n"] += 1
        b["duplicates"] += r["duplicate"]
        b["used_read_path"] += k["read_path_used"]
    return list(buckets.values())


def rederive(batch):
    """Rebuild a batch's rows from its capsules by keyless replay (no model calls)."""
    directory = OUT / ("calibration" if batch == "phase1" else f"calibration-{batch}")
    rows = []
    for path in sorted(directory.glob("*.json")):
        payload = capsules.load(path)
        got = capsules.replay_payload(payload)
        if not got["reproduced"]:
            raise SystemExit(f"{path} did not reproduce: {got}")
        rows.append(row_for(payload["summary"], payload["body"]["agent"],
                            str(path.relative_to(ROOT))))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", default="llm-naive-noread:20,llm-naive:20,llm-hardened-noread:10")
    parser.add_argument("--batch", default="2b")
    parser.add_argument("--max-usd", type=float, default=2.0)
    parser.add_argument("--start", type=int, default=0, help="first run index (parallel workers)")
    parser.add_argument("--tag", default="", help="suffix for this worker's var/ report")
    parser.add_argument("--rederive", help="print a keyless re-derivation of a batch's rows")
    args = parser.parse_args()
    if args.rederive:
        rows = rederive(args.rederive)
        print(json.dumps({"batch": args.rederive, "table": table(rows),
                          "by_key": key_table(rows), "runs": rows}, indent=1))
        return 0
    plan = [(v, int(n)) for v, n in (item.split(":") for item in args.plan.split(","))]
    live = client_for("meta-dev", "muse-spark-1.3", usd_limit=args.max_usd, purpose="calibration-2b")
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        registry, store = capsules.fresh_env(tmp)
        for variant, runs in plan:
            for n in range(args.start, args.start + runs):
                label = f"{variant}-{n:02d}"
                path = OUT / f"calibration-{args.batch}" / f"{label}.json"
                recorder = RecordingClient(live)
                result = A.run_llm(store, registry, "calibration", recorder, variant)
                capsules.write(path, capsules.build(result, recorder.records,
                                                    provider=live.provider, model=live.model,
                                                    label=label), overwrite=False)
                row = row_for(result["summary"], variant, str(path.relative_to(ROOT)))
                row["usd_so_far"] = round(live.session_usd, 6)
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("variant", "verdict", "duplicate", "claim",
                                                      "key", "error", "usd_so_far")}), flush=True)
                if row["error"] and "cap" in row["error"]:
                    break
    report = {"generated_at": datetime.now(UTC).isoformat(), "batch": args.batch,
              "model": "muse-spark-1.3", "provider": "meta-dev", "fault": A.TIMEOUT_FAULT,
              "usd_spent": live.session_usd, "model_calls": len(live.calls),
              "table": table(rows), "by_key": key_table(rows), "runs": rows, "calls": live.calls}
    out = ROOT / "var" / f"calibration-{args.batch}{args.tag}.json"
    out.write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({"table": report["table"], "by_key": report["by_key"]}, indent=1))
    print(f"usd_spent={live.session_usd:.4f} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
