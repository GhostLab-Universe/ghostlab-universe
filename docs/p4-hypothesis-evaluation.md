# P4 exploration: Hypothesis evaluation and arm notes

Milestone P4 needs a bounded exploration baseline over the frozen P1
interfaces plus a fixed-scenario arm. This note records the evaluation of
[Hypothesis](https://hypothesis.readthedocs.io/) as an optional dependency,
as required before any pin. Decision: **do not adopt for P4**.

## What was evaluated

Hypothesis stateful testing (`RuleBasedStateMachine`) against the P1
`EnvironmentAdapter`/`TargetAdapter` surface: randomized action sequences with
seeded replay, shrinking to minimal failing examples, and deadline-bounded
runs. Criteria: deterministic replay of every attempt, strict attempt/action
budgets, zero new infrastructure, and no interference with the offline
(zero live calls) milestone.

## Why not now

1. **Determinism is already served.** The P4 harness (`src/ghostlab/explore/`)
   replays every attempt from an explicit seed through `run_attempt`, and the
   fixed-scenario arm pins exact fault schedules plus scripts. Shrinking would
   be nice, not needed: the dev defect set already minimizes by hand.
2. **A new dependency needs a clean-room resolution.** Per `AGENTS.md`, a new
   pin requires resolving all dependencies together in a clean environment
   plus a committed lockfile. That is disproportionate for an offline
   milestone whose baseline (`random.Random` + explicit bounds) is ten lines.
3. **Flakiness surface.** Deadline-based health checks and example-database
   state are designed for CI tuning, not for a hermetic verdict harness. The
   P4 bar is byte-identical transcripts per seed with no wall-clock input.
4. **No effectiveness claim depends on it.** The random arm is a coverage
   baseline, not a detector: detections come from the rule library over
   recorded evidence either way.

Hypothesis is **not** pinned, not added to `pyproject.toml`, and not in
`uv.lock`. Nothing in `src/ghostlab/verify` or `src/ghostlab/explore` imports
it; `tests/verify/test_explore.py` scans those packages for banned imports.

## Revisit when

- Fixed plus seeded arms plateau on held-out scenarios and minimal failing
  examples would unblock diagnosis (then adopt behind the existing
  `AttemptSpec` seam, with a lockfile update per `AGENTS.md`).
- Stateful invariants need cross-attempt model-based checking that scripts
  cannot express.

## Guided arm status

The guided arm is **scripted-only wiring** (`run_scripted_guided`): it runs
fixed scripts through the normal campaign path so specs, assessment and
report shape are exercised, but it takes no guidance input and makes no model
call. Campaign reports label it `mode: scripted-only` with
`excluded_from_effectiveness: true`. No P4 effectiveness claim may cite it.
