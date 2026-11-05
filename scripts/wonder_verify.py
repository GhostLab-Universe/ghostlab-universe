"""Muse Wonder's proposed patch, applied verbatim as prompt text and re-run live.

    set -a; . ../ghostlab-universe/.env; set +a
    GHOSTLAB_DEV_CAP_USD=2.00 PYTHONPATH=src .venv/bin/python scripts/wonder_verify.py --live
    PYTHONPATH=src .venv/bin/python scripts/wonder_verify.py --replay demo/recorded/wonder-patch-verify.json

Arms (same tools, same timeout_after_commit on issue_refund #1, temperature 0, interleaved):
llm-naive (unpatched) vs llm-naive-wonder (naive prompt + the patch block). Then a short
adversary hunt against the patched agent under a shared exploration cap. Verdicts are the
controller's deterministic ledger checks; the patch is never executed as code.
"""

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostlab.capsule import _digest  # noqa: E402
from ghostlab.llm import adversary as ADV  # noqa: E402
from ghostlab.llm import agent as A  # noqa: E402
from ghostlab.llm import capsules as C  # noqa: E402
from ghostlab.llm.client import RecordingClient, client_for  # noqa: E402
from ghostlab.llm.spend import CappedLedger, SpendLedger  # noqa: E402

OUT = ROOT / "demo" / "recorded" / "wonder-patch-verify.json"
PURPOSE = "wonder-verify"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--replay")
    p.add_argument("--arms", default="llm-naive,llm-naive-wonder")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--max-usd", type=float, default=1.5, help="cap for the arm runs")
    p.add_argument("--hunt-usd", type=float, default=0.10)
    p.add_argument("--hunt-experiments", type=int, default=6)
    p.add_argument("--out", default=str(OUT))
    a = p.parse_args()
    if a.replay:
        out = C.replay_verify(json.loads(Path(a.replay).read_text())["payload"])
        print(json.dumps({k: out[k] for k in ("reproduced", "failed_runs", "table",
                                              "model_calls_live")}, indent=1))
        print("hunt:", json.dumps(out["hunt"] and {"reproduced": out["hunt"]["reproduced"],
                                                   "stop": out["hunt"]["summary"]["stop_status"]}))
        return 0 if out["reproduced"] else 1
    if not a.live:
        p.error("pass --live or --replay")
    arms = a.arms.split(",")
    ledger = SpendLedger()
    live = client_for("meta-dev", "muse-spark-1.3", usd_limit=a.max_usd, purpose=PURPOSE,
                      ledger=ledger)
    runs, rows = [], []
    with tempfile.TemporaryDirectory() as tmp:
        registry, store = C.fresh_env(tmp)
        for n in range(a.n):
            for variant in arms:  # interleaved so drift over time hits both arms alike
                label = f"{variant}-{n:02d}"
                recorder = RecordingClient(live)
                result = A.run_llm(store, registry, "wonder-verify", recorder, variant)
                payload = C.build(result, recorder.records, provider=live.provider,
                                  model=live.model, label=label)["payload"]
                row = C.verify_row(payload)
                runs.append(payload)
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("label", "verdict", "duplicate", "claim",
                                                      "status_check_after_timeout",
                                                      "get_order_after_timeout",
                                                      "requested_not_offered", "error")}
                                 | {"first_key": row["key"]["first_key"],
                                    "retry_key": row["key"]["retry_key"],
                                    "usd": round(live.session_usd, 5)}), flush=True)
                if row["error"] and "cap" in row["error"]:
                    raise SystemExit("spend cap hit; nothing written")
        arms_usd = live.session_usd
        hunt, hunt_usd = None, 0.0
        if a.hunt_experiments:
            capped = CappedLedger(ledger, a.hunt_usd)
            agent = RecordingClient(client_for("meta-dev", "muse-spark-1.3", ledger=capped,
                                               purpose=PURPOSE + "-hunt-target"))
            adversary = RecordingClient(client_for("meta-dev", "muse-spark-1.3", ledger=capped,
                                                   reasoning_effort="medium",
                                                   purpose=PURPOSE + "-hunt"))
            explorer = ADV.ModelExplorer(adversary, store=store, registry=registry,
                                         project_id="wonder-hunt",
                                         max_experiments=a.hunt_experiments,
                                         variants=("llm-naive-wonder",), llm_client=agent)
            state = ADV.explore(ADV.new_state(explorer, max_usd=a.hunt_usd))
            hunt = ADV.record_payload(state, adversary.records, agent.records)["payload"]
            hunt_usd = capped.spent
    table = C.verify_table(rows)
    payload = {"kind": C.VERIFY_KIND, "generated_at": datetime.now(UTC).isoformat(),
               "provider": "meta-dev", "model": "muse-spark-1.3", "temperature": 0,
               "fault": A.TIMEOUT_FAULT, "arms": arms, "n_per_arm": a.n,
               "patch": {"text": A.WONDER_PATCH, "block": A.WONDER_BLOCK,
                         "source": A.WONDER_SOURCE,
                         "applied_to": "llm-naive (naive prompt, has get_refund_status)",
                         "applied_as": "prompt text only; never executed"},
               "spend": {"arms_usd": round(arms_usd, 6), "hunt_usd": round(hunt_usd, 6),
                         "total_usd": round(arms_usd + hunt_usd, 6),
                         "source": "var/spend.json ledger, purposes wonder-verify*"},
               "table": table, "rows": rows, "runs": runs, "hunt": hunt}
    with open(a.out, "x", encoding="utf-8") as handle:  # never clobber recorded evidence
        handle.write(json.dumps({"payload": payload, "sha256": _digest(payload)}, indent=1) + "\n")
    print(json.dumps(table, indent=1))
    if hunt:
        s = hunt["summary"]
        print(json.dumps({"hunt_stop": s["stop_status"], "hunt_error": s["error"],
                          "experiments": [(e["agent"], [(f["trigger"]["tool"],
                                                         f["trigger"]["occurrence"])
                                                        for f in e["faults"]], e["verdict"],
                                           e["rules_failed"]) for e in s["experiments"]],
                          "hunt_usd": hunt_usd}, indent=1))
    print(f"total_usd={arms_usd + hunt_usd:.4f} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
