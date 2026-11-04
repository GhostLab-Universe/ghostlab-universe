"""Calibration of the LLM refund agent under timeout-after-commit (live, recorded).

    set -a; . ./.env; set +a
    PYTHONPATH=src .venv/bin/python scripts/calibrate.py --runs 5 --max-usd 5
    PYTHONPATH=src .venv/bin/python scripts/calibrate.py --glimmer   # one naive-glimmer run

Cells: {naive, hardened} x {read path, no read path} on muse-spark-1.3, N runs each.
Every run is a controller run with a recorded capsule under demo/recorded/calibration/.
Nothing is tuned to force failures; whatever the agent does is the finding.
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
from ghostlab.llm.client import RecordingClient, client_for, provider_status  # noqa: E402

OUT = ROOT / "demo" / "recorded"
CELLS = [("llm-naive", "naive", "read"), ("llm-naive-noread", "naive", "noread"),
         ("llm-hardened", "hardened", "read"), ("llm-hardened-noread", "hardened", "noread")]


def one_run(env, live, variant, label, path):
    recorder = RecordingClient(live)
    result = A.run_llm(env[1], env[0], "calibration", recorder, variant)
    capsule = capsules.build(result, recorder.records, provider=live.provider,
                             model=live.model, label=label)
    capsules.write(path, capsule)
    s = result["summary"]
    return {"variant": variant, "capsule": str(path.relative_to(ROOT)), "run_id": s["run_id"],
            "status": s["status"], "verdict": s["verdict"], "duplicate": s["duplicate_refund"],
            "claim": s["claim_check"]["verdict"], "model_calls": s["model_calls"],
            "error": s["error"], "stop": s["stop"],
            "tools": [a["tool"] for a in s["gateway_actions"]]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--max-usd", type=float, default=5.0)
    parser.add_argument("--cells", default="all")
    parser.add_argument("--glimmer", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        env = capsules.fresh_env(tmp)
        if args.glimmer:
            status = provider_status("glimmer-local")
            print(json.dumps(status))
            if status["status"] != "available":
                return 1
            live = client_for("glimmer-local", purpose="calibration-glimmer")
            row = one_run(env, live, "llm-naive", "naive-glimmer", OUT / "naive-glimmer.json")
            row["latency_ms"] = [c["latency_ms"] for c in live.calls]
            (OUT / "glimmer-run.json").write_text(json.dumps(row, indent=1) + "\n")
            print(json.dumps(row, indent=1))
            return 0
        live = client_for("meta-dev", "muse-spark-1.3", usd_limit=args.max_usd,
                          purpose="calibration")
        rows = []
        cells = CELLS if args.cells == "all" else [c for c in CELLS if c[0] in args.cells]
        for variant, prompt, read in cells:
            for n in range(args.runs):
                path = OUT / "calibration" / f"{variant}-{n}.json"
                row = one_run(env, live, variant, f"{variant}-{n}", path)
                row["usd_so_far"] = round(live.session_usd, 6)
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("variant", "verdict", "duplicate", "claim",
                                                      "model_calls", "error", "usd_so_far")}),
                      flush=True)
                if row["error"] and "cap" in row["error"]:
                    break
    table = []
    for variant, prompt, read in cells:
        cell = [r for r in rows if r["variant"] == variant and not r["error"]]
        errors = [r for r in rows if r["variant"] == variant and r["error"]]
        n = len(cell)
        table.append({"variant": variant, "prompt": prompt, "read_path": read, "runs": n,
                      "errored_runs": len(errors),
                      "duplicates": sum(r["duplicate"] for r in cell),
                      "false_success": sum(r["claim"] == "false_success" for r in cell),
                      "no_report": sum(r["claim"] == "no_report" for r in cell)})
    report = {"generated_at": datetime.now(UTC).isoformat(), "model": "muse-spark-1.3",
              "provider": "meta-dev", "fault": A.TIMEOUT_FAULT,
              "usd_spent": live.session_usd, "model_calls": len(live.calls),
              "table": table, "runs": rows, "calls": live.calls}
    (OUT / "calibration.json").write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps(table, indent=1))
    print(f"usd_spent={live.session_usd:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
