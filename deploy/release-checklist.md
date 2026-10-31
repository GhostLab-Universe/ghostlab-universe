# Release checklist

There's no automatic deployment, and nothing here deploys. A human works through this
list and decides. The output is a release record with three separate statuses. Passing
one says nothing about the others.

Targets are in [release-targets.json](release-targets.json).

| Status | Today | What moves it |
| --- | --- | --- |
| Runtime | Local development only | An authenticated service (M07), then the hosted checks below |
| Dependency | Locked: Python 3.13.18, uv 0.13.6, Node 24.22.1 | A reviewed M00 lock change |
| Live capability | **Not started** | A sanitized M01 compatibility report from an authorized live run. Never a mocked result |

## Before promotion

All of these must hold. Any failure blocks promotion.

1. [ ] The candidate is a reviewed merge head, recorded as a full commit id.
2. [ ] All jobs in `.github/workflows/ci.yml` passed for that commit, including
   `promotion-gate`. This needs a repository admin to make `promotion-gate` a required
   status check; until then the block is procedural.
3. [ ] Clean-clone validation on the candidate:
   - `uv lock --check`
   - `uv sync --locked --all-extras --group dev`
   - `uv run --frozen pytest`
   - `uv run --frozen python -m ghostlab.contracts.export --check`
   - `PYTHONPATH=src python -s -m unittest discover -s tests` (14 core tests)
   - `npm --prefix web ci`, `contracts:verify`, `contracts:typecheck`
   - refund and journey demo and replay, 0 model calls
4. [ ] `python -m evals.run --out <dir>` exits 0. Any failed expectation or fingerprint
   drift blocks. If behaviour changed on purpose, update `evals/expected.json` with
   `--write-expected` in its own commit, and have a reviewer read the diff. CI never
   writes that file.
5. [ ] `python -m evals.release --evidence <dir> --commit <sha>` exits 0, and the record
   is attached to the release notes. Run it from a clean checkout of `<sha>`: evidence from
   another commit or a tree with local changes blocks.
6. [ ] The recovery drill passed on this candidate ([recovery-drill.md](recovery-drill.md)).
   The automated case `ops.recovery_drill` is part of step 4.
7. [ ] The stores were backed up before upgrading (the drill's backup step).
8. [ ] The rollback target is still correct. A release that adds a migration needs a
   backup taken before it, because migrations are forward-only.
9. [ ] No skipped test in `tests/integration/test_m09_full_application.py` is claimed as
   passing. They're M07/M08 placeholders.
10. [ ] Provider-held data: Meta `store` defaults to `true` and continuation relies on it
    (see M01.md). Record the retention decision before any live use.
11. [ ] Model review, if any, is commentary only. It can't replace a deterministic check
    or waive a failure.

## Before customer-connected use

Not reachable today. It needs everything above, plus gates 1–4 of
`docs/acceptance-gates.md` where they apply, and gate 5's live evidence.
`customer_connected_use_allowed` in a release record is false for a local target whatever
else the record says. A future target of kind `customer_connected` also needs an
authenticated runtime, evidence for every security gate, a documented provider, endpoint
and model, and a real compatibility report that passes the controller's own check and
shows timeout, cancel, rate-limit and budget handling were probed. Today's probe doesn't
cover those, so `true` is unreachable, on purpose. A status or a path in the targets file
proves nothing. `customer_connected_blockers` lists what's missing, and none of this changes
offline promotion.

## After promotion

- [ ] Run the demo and replay on the promoted commit.
- [ ] Check telemetry is being written and stays within its size bound.
- [ ] Watch for a recurring failure. The loop is: recurring failure → reproducible case in
  `evals/cases.py` → reviewed prompt, skill or code version → regression results →
  controlled release → monitoring or rollback.

## Rollback

Triggers: a failing invariant on the promoted commit, a capsule that no longer replays,
a secret in telemetry, or an unexplained budget overrun.

1. Stop the local coordinator. No campaign should be running.
2. Check out `rollback_target.commit` and sync with `uv sync --locked`.
3. Restore the pre-upgrade store backup. Code rollback alone fails with "Database schema
   is newer than this code" once migration 0003 has been applied.
4. Keep the capsules and evaluation evidence from the failed release.
5. Run the checklist's step 3 on the rollback commit.
6. Record what happened and add a reproducible case before trying again.

Campaigns started after the backup are lost by a restore. Their capsules, if exported,
still replay offline.
