# P6: application workflow, CI and measured demonstration

## What a developer does

1. Open **Extensions** in the UI (or call `/v1/extensions/*`).
2. Pick a connected environment; read its tools, faults, invariants,
   fixtures, capabilities and registered targets.
3. Select a fixture and read its snapshot for task identities.
4. Run **qualification** (interface conformance) and **fidelity**
   (determinism, replay, reference diffs, declared approximations).
5. Connect a target: a registered id or an inline deterministic script.
6. Run a **campaign**, **inspect** a capsule, **replay** recorded evidence,
   **retest** a changed target, **minimize** faults, and download the
   regression case.

Replay, retest and enforcement render as three distinct outcomes
everywhere; a retest never claims replay equivalence.

## Endpoints

`GET /v1/extensions/environments` (list) and
`GET /v1/extensions/environments/{world}` (describe) plus
`.../fixtures/{name}` (snapshot) expose the operator registry, which
serves the reviewed local adapters (refund, reservation). `POST`
`qualify`, `fidelity`, `campaigns`, `inspect`, `replay`, `retest` and
`minimize` run the P2/P4/P5 libraries synchronously with API-tightened
bounds. Worlds here are synthetic fixtures; probe actor ids are
caller-declared test identities. Unknown worlds are 404, invalid
requests 422, oversize envelopes 413, world refusals 403.

## CI

`extension-regressions` runs `tests/replay`, the demo-agent tests,
`scripts/run_regressions.py examples/regressions/ext` (saved evidence
must replay with the expected verdicts) and the measured demo. Any
mismatch fails the job.

## Measured demo

```
uv run --frozen python scripts/measured_demo.py --out demo-out/measured-demo
```

One external environment (`reservation-world/1`, service developed
outside this tree and vendored), two dependency-free external agents
(`examples/p6_demo_agents.py`, zero imports): a blind booker that
over-commits capacity and a careful booker that checks availability and
abstains on stale reads. The script qualifies the world, detects the
genuine `capacity_respected` violation, reproduces it on a fresh
instance, minimizes three faults to `skip_capacity_check` with the same
witness, retests the fixed agent clean, and checks stability across
three repeats. Zero inference calls.

`reservation-world/1` is not a frozen pack version, so the demo records
evidence JSON rather than a v3 capsule; world-version extensibility is
an explicit M00 follow-up. Held-out evaluation stays **pending**: no
runner has been published by its owner, and dev results are never
substituted.
