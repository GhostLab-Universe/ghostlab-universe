# TODO.md — GhostLab Universe

Read at session start and updated at session end. Deadlines: feature freeze **02:00 IST**, submission closes
**07:30 IST, Nov 5 2026**.

---

## Now — Phase 1: live Meta models

- [ ] Key-label mapping: build `ProviderConfig` from `META_API_KEY_DEV` (local) or `META_API_KEY_PUBLIC`
      (public deploy only) by explicit label. Do not rename keys in `.env`, and never print values.
- [ ] Per-call usage ledger: record model, provider, key label, input and output tokens, cost estimate and request id for every
      live call. Keep running totals and refuse calls once DEV reaches USD 120 or PUBLIC reaches USD 50.
- [ ] Re-run `scripts/probe_provider.py --provider meta_responses --live --model muse-spark-1.3 --max-calls 4
      --confirm-paid-calls` on the DEV key, and keep the redacted report in `artifacts/local/`.
- [ ] `ModelExplorer.propose(findings) -> list[dict]` in `experiments/plan.py` or next to it. It calls
      `MetaResponsesAdapter.complete` with structured output, translates the result to v1 requested-config bodies, and lets
      `ops` reject invalid ones. Use `max_output_tokens` 2048.
- [ ] Ollama Muse Glimmer adapter (http localhost, Windows Ollama) as the free local explorer. It needs its own
      config, because `ProviderConfig` is https-only.
- [ ] Expose `model_guided` planning through the API/UI (step 6 currently shows "explorer: none").

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

- [x] Phase 0: inventory, `hackathon/final` branch, provider port, docs in the template kit, code-review-graph — 2026-11-04
