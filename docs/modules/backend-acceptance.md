# Backend acceptance: booking example through the execution-backend seam

Executable acceptance:

```bash
python examples/equipment_booking/backend_acceptance.py
```

Expected: `ACCEPTANCE PASS (9/9 stages passed; model_calls=0)`, exit 0.
It runs the equipment-booking example end-to-end (register -> import ->
qualify -> connect agent -> run -> violation -> retest) and proves the
backend seam behaves honestly:

| Stage | What it proves |
|---|---|
| env-qualify | Booking env qualifies for declared capabilities, digest pinned |
| trusted-local-baseline | Vulnerable agent duplicates (rule violation caught); repaired agent passes; `model_calls=0` |
| backend-seam-run | Same run via the local reference backend; evidence identical to trusted-local |
| unknown-backend-refusal | Resolve refuses unknown backend names; no run record, no fallback |
| unavailable-backend-refusal | Known-but-unavailable backend refuses at start (HTTP 503 at API layer); receipt `prepared->failed`, no budget spent |
| qualification-honesty | Local backend reports `unqualified`; no backend claims sandbox isolation it has not proven |
| cancellation | Preset cancel and direct cancel both confirmed; evidence marked incomplete honestly |
| evidence-identity | Receipt package pinned to adapter digest; observed vs claimed kept separate; cross-project read refused |
| determinism-zero-model-calls | All runs deterministic, `model_calls == 0` everywhere |

Deterministic simulation is real execution, not live model inference.
No live inference is authorized or used here.
