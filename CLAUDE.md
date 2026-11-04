# CLAUDE.md — GhostLab Universe

GhostLab is a developer tool that finds and reproduces failures in stateful agent workflows caused by timing,
retries and changing permissions. Built for the Meta hackathon. Rules live in `AGENTS.md`; this file holds gotchas only.

## Gotchas

**Environment (WSL)**
- Local uv is 0.12.x, but `pyproject.toml` pins `required-version >=0.13.6`. Pass `uv --no-config ...`
  (or run `uv self update`, which is free).
- System `python3` is 3.14 and the project needs 3.13. Always use `.venv/bin/python`.
- `web/package.json` wants Node ≥24.22.1, and WSL has 22.x. The EBADENGINE warning is harmless, and tsc/vite still pass.
- Ollama is the **Windows** install (`/mnt/c/Users/drona/AppData/Local/Programs/Ollama/ollama.exe`). It is not in
  WSL PATH. Reach it over HTTP, not through a CLI in WSL.
- The repo is **public**. Anything committed is published.

**Tests**
- `tests/providers/` has no `__init__.py`, so `unittest discover -s tests` silently skips it. Use pytest.
- Two tests fail on Linux only. They were authored on Windows and are not regressions:
  `test_backends_b1::test_env_scrubbing` (`LC_CTYPE` set by PEP 538 coercion) and
  `test_backend_adversarial_b2::test_traversal_via_outputs` (`sub/..` with a missing `sub/` doesn't resolve on POSIX).
- `test_e2e_proof_t4` and `test_triggers_minimize_e2` shell out to `python -m pytest`. Without pytest in
  the venv they fail with a misleading "guard did not fail" message.
- `tests/test_backend_docker_b4.py` is excluded. Its daemon tests are cut (see TODO).

**Models and providers**
- `providers/` and `security/` were ported verbatim from the integration line (`9f5162d`). Nothing else on
  this branch calls them yet.
- `ProviderConfig.from_env` reads `MODEL_API_KEY` and `GHOSTLAB_META_MODEL_ID`, while the real keys are named
  `META_API_KEY_DEV` / `META_API_KEY_PUBLIC`. Map them explicitly by key label and never copy a value into another env var file.
- `ProviderConfig` refuses non-https `base_url`, so Ollama (`http://localhost:11434`) needs its own adapter
  and cannot reuse the Meta config.
- `muse-spark-1.3` passed the Meta probe 5/5 on 2026-11-01 (integration line, `docs/modules/M01.md` there).
  Structured output at `max_output_tokens=512` returned `incomplete`. Use 2048.
- The planner's `model_guided` strategy (`experiments/plan.py`) wants `explorer.propose(findings) -> list[dict]`,
  where each dict is a **v1 requested-config body**. `contracts.ExperimentProposal` is refund-shaped and is
  **not** that shape. A model explorer has to translate, and every proposal is re-validated through `experiments.ops`.
- `targets/gateway.py` mediates only calls made through `dispatch()`. A world held directly can still be called.

**Code layout**
- There are two `ARCHITECTURE`-ish docs. `ARCHITECTURE.md` is the summary and `docs/architecture-decisions.md` holds the
  cited record. Never recreate lowercase `architecture.md`, because it collides on Windows/macOS.
- The web UI is `React.createElement` in `.ts` files (`web/src/screens/step1-7.ts`). There is no JSX and no React plugin.
- The controller's `drive_agent` is refund-shaped. Booking and proxy runs go through walkthrough/qualify/proof
  paths, not the generic run endpoint.
- Run-group handles and effective configs live in process memory, so a service restart drops them.

## Session

Start: read `SESSION-LOG.md`, `TODO.md`, `ARCHITECTURE.md`. End: tests, tsc, build pass; update
`SESSION-LOG.md` and `TODO.md` with the agent name.
