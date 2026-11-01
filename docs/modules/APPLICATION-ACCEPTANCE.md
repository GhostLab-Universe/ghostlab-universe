# Application acceptance candidate

Separate branch codex/application-acceptance. The candidate combines integration d1fe76f
(merged M07 b2c4390 and M08 a3e7b1a) with pending M09 291a0cc. No parent branch or
integration ref is changed. Review and merge M09 #18 before promoting this follow-up.

The six skipped full-application placeholders are replaced by actual checks:
- Python API acceptance: refund flow; journey default and delivery race; token-derived
  project isolation for campaigns, approvals, artifacts and replay; secret-safe errors;
  and byte-for-byte agreement between the service OpenAPI and the frontend's copied contract.
- Real browser acceptance: production frontend and real `python -m ghostlab.api --scripted`
  process; refund and journey race through create, inspect diff, approve, verify repair,
  export and replay. Browser requests/responses and production assets are scanned for a
  configured synthetic provider canary. There are no intercepted/mocked browser routes.

These tests execute real worlds, SQLite, deterministic verifiers, policy, approval workers
and replay. Proposals are checked-in scripted responses, not a live model. The browser
server drops provider keys before serving. A canary scan proves that canary was absent;
it is not a proof about every possible secret or every production configuration.

Run the API checks with the locked Python environment:
`python -m pytest tests/integration/test_m09_full_application.py`

From web/, after npm ci on the pinned Node:
`node node_modules/@playwright/test/cli.js test --config tests/browser/application.config.ts`

Set GHOSTLAB_ACCEPTANCE_PYTHON to the locked environment's Python when it is not on PATH.
Default is Playwright Chromium. GHOSTLAB_BROWSER_CHANNEL=chrome uses installed Chrome.
GHOSTLAB_APPLICATION_OUT selects an isolated store/build/results directory. Both servers
bind loopback on separate test ports, refuse port reuse, and use no user database. The
runner shuts down its own processes. A rerun should use a fresh output directory.

CI adds an application-acceptance job that installs the locked dependencies and Chromium,
runs these checks and retains browser results. The promotion job depends on it. The
workflow file is tested locally but the new job has not yet run on GitHub.

Recorded local evidence, Windows/Python 3.13.2/pinned portable Node 24.22.1:
- Combined API, full-application acceptance and CI configuration checks: 292 passed.
- Real installed Chrome on the production build: 3 passed (refund, journey race, canary).
- A prior full Python suite run produced 2183 passes and two failures caused by ambiguous
  global conftest imports. Test helpers now use package-qualified imports; both failing
  cases passed in both collection orders, and the affected combined suite above passes.
  The entire Python suite has not been rerun after that fix.
- Frontend application/test TypeScript checks and generated API drift check: pass.
- Model inference: scripted only for application acceptance.
- Separately, one authorized direct Meta connectivity call returned HTTP 200/incomplete;
  docs/evidence/meta-connectivity-2026-11-01.json records its bounded result. It does not
  pass the live compatibility gate or enable customer-connected use.

Not covered: real browser sign-in, independent reviewer browser login, provider-retention
qualification, live-model application behavior, Service Relay, imported external documents,
unknown agent code, or hosted deployment. Pending security/minimizer drafts are not merged
into this candidate. GitHub CI and a Linux browser run remain unverified locally.
