# TODO.md — GhostLab Universe

Read at session start and updated at session end. Deadlines: feature freeze **02:00 IST**, submission closes
**07:30 IST, Nov 5 2026**.

---

## Now

- [ ] Record the 3-minute video from `/guide/6-run` at 1920×1080 (replay mode, keyless).
- [ ] Rerun `node web/tests/acceptance.mjs --service=...` after the step 6 restructure.
- [ ] Glimmer-vs-Spark (`/v1/models/compare`) is not shown in the UI yet. Add it to the Evidence view if time allows.
- [ ] Glimmer: try a Modelfile with an explicit chat template so Chat Completions returns content, then record one naive run.
- [ ] Fairer benchmark: opaque agent variant names, booking env plus refund env, and record each trial's chosen cell.

## Next

- [ ] Rewrite README for judges: what it is, a 3-command run, the proof scripts, and an honest claims list.
- [ ] Choose a free, no-card public deploy that uses only the PUBLIC key.
- [ ] Run `demo-ready` browser acceptance (`node web/tests/acceptance.mjs --service=...`) on WSL.

## Later

- [ ] Final trust-boundary report (`docs/modules/backend-final-report.md`, never written).
- [ ] Independent security review of `src/ghostlab/backends/`.

## Bugs

- [ ] `tests/test_backends_b1.py::LocalBackendTests::test_env_scrubbing` fails on Linux because `LC_CTYPE` (PEP 538
      locale coercion) appears in the child env. The fix is probably to allow `LC_CTYPE` in the test's
      allowed extras, or to scrub it in `backends/local.py`.
- [ ] `tests/test_backend_adversarial_b2.py::test_traversal_via_outputs` fails on Linux because the attack path `sub/../../x`
      raises FileNotFoundError when `sub/` doesn't exist (Windows normalizes it). This is test-side: create `sub/` first.
- [ ] README describes the Oct 29 core and the two-person team, so it is stale.
- [ ] `tests/test_api_e4.py::test_body_cap_json_and_no_registration` fails on WSL (a broken pipe on an over-cap body), and
      `test_runs_budget_409_cancel_and_cross_project` is timing-flaky (2/6 runs). Both are pre-existing at c1c5040.

## Cut

- Docker B4 daemon-enforced tests (`tests/test_backend_docker_b4.py::DaemonEnforcedTests`). They are slow, need the
  daemon, have known test-side bugs, and only matter for a reviewed-code isolation claim that we won't make
  before the deadline.
- Best Creative prize. Effort goes to the core proof and live models, not a separate creative entry.
- Baseten, Modal and Fireworks. There are no credits, and they need a card. The Baseten adapter stays in code, unused.
- Postgres. SQLite serial MVP is enough for a single-coordinator demo. Hosted multiworker is not needed.
- Hosted runner (outbound-authenticated developer-side runner). Out of scope for the deadline, and it requires infra we can't pay for.
- Merging PR #49/#24/#22/#1. They are on a diverged line with conflicting designs (see ARCHITECTURE decisions). Their optional sign-in
  and GitHub inspection went with them.

## Done

- [x] Phase 2A: demo UI on the live-model routes. Step 6 has Hunt, Failure (Fork Reality), Diagnosis, Fix & retest, Capsule replay and Evidence, all keyless in
      replay (2026-11-04, Claude Code session 2A).

- [x] Phase 1: live Meta models. This covers the spend ledger, clients, the LLM agent, claimed-vs-actual, calibration, the adversary, diagnosis, keyless replay,
      API, and benchmark (2026-11-04, Claude Code).

- [x] Phase 0: inventory, `hackathon/final` branch, provider port, docs in the template kit, code-review-graph — 2026-11-04
