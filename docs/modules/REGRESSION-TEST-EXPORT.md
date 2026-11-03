# Regression-test export

Turn a saved minimization result into a runnable pytest module. Additive
milestone feature: it reads the frozen regression dicts, reuses the frozen
replay/classify/runner code by import, and edits none of them.

## Inputs

- `ghostlab-diagnostic-regression/2` (`regression-case.json` from
  `src/ghostlab/diagnostics/minimize.py`), plus the sibling capsule file its
  `capsule` field names. The capsule must be a refund v1 or journey v2
  capsule whose bytes match the case `sha256`.
- `ghostlab-diagnostic-regression-ext/1` (the `regression` dict from
  `src/ghostlab/replay/minimize_ext.py`), plus `--evidence` recorded
  evidence shaped exactly like the `scripts/run_regressions.py` case fields
  (`world_version`, `fixture`, `scenario`, `actor_id`, `trace`, `state`,
  `checks`) with no schema tag. No new contract versions are introduced.

## Run it

From a checkout, inside the locked venv (`uv sync --frozen --all-extras`):

```text
python scripts/export_regression_tests.py path/to/regression-case.json --out tests/regressions/test_refund_timeout.py
python scripts/export_regression_tests.py path/to/ext-regression.json --out tests/regressions/test_ext_case.py --evidence path/to/ext-evidence.json
python -m pytest tests/regressions/test_refund_timeout.py -q
```

Exit 0 prints a JSON summary; exit 2 refuses the input with one line on
stderr (no traceback). Output files are never overwritten.

## What the generated module asserts

Semantics mirror `scripts/run_regressions.py`: replay must reproduce the
recorded evidence or the test fails. Every export is pre-flighted, so a
generated module passes on the tree that produced it and fails only when
behavior changes:

- `test_replay_reproduces_recorded_evidence` -- recorded trace, final state
  and checks replay exactly on a fresh world (`model_calls == 0`).
- Violation cases (`expected_passed: false`, or any ext dict) additionally
  lock the SAME failure: same invariant plus the identical failure witness
  (frozen `classify`/`failure_witness` for capsules, exact witness equality
  for extension evidence). A fix, a different failure, or unreplayable
  evidence fails with the verdict, witness digest, and checks named.
- Fixed cases (`expected_passed: true`) assert the invariant holds and fail
  with `REGRESSION: violation of <invariant> recurs` when it comes back.

Generated modules are standalone: capsule cases need only the ghostlab
package's standard-library path plus pytest; extension cases need the locked
venv. Each module also runs under bare `python <file>` (PASS/FAIL/SKIP
lines, exit 0/1) and embeds no absolute paths.

## Unknown and incomplete, reported explicitly

- An unfinished ext search (`faults: null`) or an ext dict without
  `--evidence` exports a module whose single test calls `pytest.skip` with
  the reason. Unknown is reported, never a silent pass or a faked test.
- Unknown worlds fail closed: extension replay uses a fresh registry with
  ONLY `register_refund_adapters` and `register_reservation_adapters`, and
  capsule replay runs the named checked-in adapter and refuses unknown ones.
- Generation refuses to emit a test that would fail here (tampered witness,
  sha mismatch, non-reproducing evidence, expectation incoherence), with the
  cause named. Truncated recordings replay deterministically; any mismatch
  fails with the `matches`/`divergence` detail.

## Guarantees and limits

- Zero model calls: replay and fresh runs are deterministic local
  simulations; the suite runs exports and generated modules with the network
  blocked.
- No credentials: every embedded payload is scanned for credential-shaped
  keys (`api_key`, `secret`, `password`, tokens, ...) before it is written;
  a hit refuses the export.
- Case ids are sanitized to identifiers; exports are byte-deterministic for
  identical inputs; capsules larger than 512,000 bytes are refused with an
  explicit error rather than embedded.
- Limits: extension replay covers only worlds with reviewed registered
  environments (refund, reservation); journey cases replay through the
  frozen capsule path. The ext pre-flight imports `run_case` from
  `scripts/run_regressions.py`, so ext export needs a repository checkout.
