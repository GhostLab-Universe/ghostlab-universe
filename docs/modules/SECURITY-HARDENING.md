# Independent security hardening

1 November 2026. Branch `codex/security-hardening`, based on M07 `c19fe9d`.
This is a separate worktree. It does not change Claude's implementation branches,
locks, engine, world manifests or M09 release switch.

## Authentication integration

`ghostlab.api.introspection.IntrospectionSessions` implements the existing
`SessionAdapter` protocol using [OAuth token introspection (RFC 7662)](https://www.rfc-editor.org/rfc/rfc7662).
The authorization server validates opaque access tokens. This application requires
`active: true`, issuer, audience, expiry, subject and the `ghostlab` scope; it also
checks any `nbf` and `iat` and requires `token_type: Bearer`. Those fields are an explicit stricter deployment profile,
not a claim that RFC 7662 requires every field.

The server maps the issuer's subject to a principal and one project. Token/body
project and role claims do not grant membership. Membership configuration is an
operator-controlled allowlist; changing it requires replacing/restarting the adapter.
Revocation at the issuer is checked on every request. Introspection has no cache or
retries, bounded response size, bounded concurrency, verified HTTPS, no redirects and
no environment proxy inheritance. The ASGI middleware awaits native async introspection. A cancellation deadline covers
connect, headers and body reads together; it does not strand a blocking request thread.
Responses with Content-Encoding other than identity are refused before reading or decoding
content. Accepted JSON is capped at 64 KiB and 64 nesting levels. Eight IdP operations may
run concurrently; overload and IdP failures return retryable JSON 503 with Retry-After,
not invalid-token 401. An inactive token or failed claim check returns 401. Junk bearer
tokens can occupy the same bounded slots; this is not a rate limiter or a DoS defense.
The real IdP still needs deployment-level rate limits. Request-scoped HTTP clients close
on completion/cancellation, and API shutdown closes the session adapter. Wrappers must
forward authenticate_async and close to retain native async operation and cleanup.

Use an authorization server supporting RFC 7662 and client-secret-basic with this
profile. A provider that only publishes JWT signing keys needs a different verifier;
do not send those tokens to an arbitrary introspection endpoint.

Environment names read only by explicit `from_environment` configuration:

- `GHOSTLAB_OIDC_ISSUER`
- `GHOSTLAB_OIDC_INTROSPECTION_URL`
- `GHOSTLAB_OIDC_AUDIENCE`
- `GHOSTLAB_OIDC_CLIENT_ID`
- `GHOSTLAB_OIDC_CLIENT_SECRET` (server-side; never a frontend variable)

Construct `ProjectMember` entries for the actual issuer subjects and pass the configured
adapter and project approver authority to `configure_introspection(config, sessions,
approvers=authority)`. The operator must also configure the matching synthetic world
bindings in `Services`. The helper installs independent-review approval policy.

**No IdP has been chosen or connected.** Tests use `httpx.MockTransport` as a labelled
identity-provider fixture. They exercise the actual API, SQLite and policy, but do not
prove an external identity provider works. No login/registration screen or redirect
flow is added. Browser login is a subsequent integration with the selected IdP.

The service remains loopback-only. `production_authenticated` remains false because
that field is the existing service's deployment claim. The new `auth_mode` is
`oidc_introspection`, so clients can distinguish externally validated tokens from
configured demo tokens. Do not expose this service publicly by editing that flag.
TLS termination, trusted proxy/Origin configuration, rate limiting, account lifecycle
and a deployment review remain prerequisites to hosting.

M07/M08 interface change: one new `AuthMode` enum value and a regenerated OpenAPI file.
No existing response field is removed. M08 must regenerate its client after the M07
owner accepts this addition.

## Separate reviewer

`SeparatedApprovalService` requires a human with project approval authority and a
different principal ID from the requester. Self-approval leaves the request pending.
Consumption also rechecks the stored reviewer, so a legacy self-approved request
cannot bypass the new policy after a configuration change. The IdP configuration helper always installs this service; `create_app` refuses an
introspection adapter paired with the legacy approval service. The middleware also checks
the resolved auth_mode, so wrapping that adapter cannot bypass the separate-review rule.
Project-authorized human reviewers can discover pending requests with GET /v1/approvals
(limit 1..100, exclusive after cursor); other projects are never listed. The explicitly labelled
single-user demo retains its existing policy so the deadline demo keeps working.

Distinct IDs must correspond to independently administered human identities. Two
accounts controlled by the same person do not establish organizational separation of
duties. The IdP and the operator own that assurance. The service's `can_approve` means
general reviewer eligibility; the action-specific self-approval check occurs on decision.

## Adversarial documents

`document_observation` loads only project-owned UTF-8 `text/plain` artifacts. It checks
evidence-read capability, project ownership, digest and a 32 KB read limit. Known secret
patterns are redacted; the host can inject its configured M06 redactor for literal
secrets. Content reaches M03 only as an untrusted observation, never instructions.

No document-upload route, URL fetching, PDF/office parser or HTML rendering is added.
The tests cover the store-to-context boundary and cross-project refusal. This closes
that adapter's test gap; it is not full application document-ingestion acceptance.
M07/M09 must test the actual route if document ingestion is later exposed.

## Provider retention decision

**Live inference through this draft API is disabled for every session adapter**, including
TokenSessions, wrapped introspection, and configurations containing an otherwise valid M05
live authorization. Start/resume and repair-resume return a typed 403
live_content_not_qualified before a worker launches when live authorization is configured.
With no live authorization, the existing inference_not_authorized refusal is preserved. A second factory boundary refuses
complete() if a call reaches the adapter. Scripted execution and exact httpx.MockTransport
fixtures remain available. Existing evidence can be read and campaigns can be cancelled;
an unused approval is not consumed by a refused resume. SessionInfo reports disabled for
the blocked live configuration. Recorded campaign origins remain historical evidence.

This deliberately withdraws the earlier live-enabled scope. A synthetic label is merely
an operator assertion, not a content classifier or data-loss-prevention control. No claim
is made that a name, phone, identity number or other personal content can reliably be
identified from arbitrary text. This branch does not ship a qualified outbound-content
boundary, so neither a label nor a retention record may release live content. The M01
standalone probe and other CLI paths are outside this API change and retain their own gates.
Coordinate this restriction with the live-demo owner before merging.

RetentionDecision is now metadata validation only: exact provider/endpoint/model, reviewer,
policy URL, timezone-aware date, verified account controls, and a 30-day expiry. For Meta,
continuation_policy must explicitly record stateful-provider-storage-reviewed or
stateless-provider-storage-reviewed. This records a review choice; it does not change a
wire setting or prove ZDR. Metadata failures use RetentionRefused (HTTP 403 with a fixed
reason), never an unmapped ValueError. RetentionCheckedFactory returns an adapter whose
complete() is disabled even when the metadata passes. Future live support needs reviewed
request/content provenance enforcement and tests at the actual outbound boundary.

Local memory/checkpoints/artifacts retain their current behavior. No deletion scheduler,
pruning procedure or local retention window is implemented by this branch. Before real
content is accepted, a separate policy must define access, retention, deletion, backups
and audit evidence and prove its implementation. Do not infer such a procedure from M09
or from these documentation links.

## Customer-connected use

M09's `customer_connected_use_allowed` stays false. This branch provides implementation
and test evidence for a subset of the blockers, not permission to erase them. Outstanding
external checks include the real IdP, account-backed memberships/reviewer assignment,
browser login, provider controls, exact-model live compatibility, integrated acceptance
and deployment/evidence-origin qualification. Keep each blocker tied to its own proof.

## Validation

Results are recorded in the branch's handoff after tests finish. All token responses,
model outputs and credentials supplied by these tests are synthetic. No real IdP or
inference provider is contacted and no paid call is made. No real key is needed.

Earlier-head results (a77b37a), Windows, Python 3.13.2, existing locked environment:

- Selected API/policy/context/core regression run during development: **521 passed,
  6 subtests passed**. Command: `pytest tests/security_hardening tests/policy tests/api
  tests/context tests/test_core.py`.
- After the final security edits: **39 focused tests passed**. Command: `pytest
  tests/security_hardening tests/api/test_hardening_auth.py`.
- `python -m ghostlab.api.openapi --check`: no drift.
- `python -m ghostlab.contracts.export --check`: no shared schema/example drift.
- `git diff --check`: clean.

The environment initially blocked the Windows socketpair needed to start TestClient.
The affected test processes were stopped and rerun with loopback enabled. Their IdP
transport and model transport remained mocked. No temporary test workaround was added
to application code. This was not a fresh-clone install, full M09 run or browser test.

## Handoff to Claude's active tracks

1. Review this branch separately. Its base is M07 `c19fe9d`; do not replace M07/M08/M09
   branches with this worktree. Retarget after the parent M07 PR merges.
2. M08: regenerate from the updated `openapi.v1.json` after acceptance. Add the
   `oidc_introspection` label; do not interpret it as deployment qualification.
3. Select an IdP supporting the documented introspection profile. Configure real
   subject memberships, client credentials and independent reviewers server-side.
4. Add that provider's login integration and verify actual token lifecycle. No IdP
   account or external configuration has been created by this branch.
5. M09: consume these tests as evidence for their exact boundaries. Do not mark full
   document ingestion, production deployment or customer-connected use as passed.
6. Preserve the existing local demo. Hosted access and paid inference remain separately
   gated; the release switch is unchanged.

## Review correction scope (November 2026)

The original review correctly found session-type-dependent retention enforcement, a false
content-refusal claim, an ineffective wall-clock check, decompression before size checking,
and missing lifecycle/error handling. The live-enabled claim is withdrawn, not relabelled
as a passing privacy feature. Regression evidence and final validation are recorded in the
updated PR description. This is not an IdP deployment, live Meta qualification, production
authentication certification, or a claim that all externally generated mutants were killed.

Final review-fix validation (Windows, Python 3.13.2, existing locked environment):

- `pytest tests/security_hardening tests/policy tests/api tests/context tests/storage tests/test_core.py -q`:
  **655 passed, 6 subtests passed**. This includes the API process/loopback tests.
- New review probes cover compressed 64 MiB expansion rejected before body read,
  cancellation during headers/body, 12 concurrent tokens (8 accepted operations and
  4 retryable capacity refusals), real redirect targets, wrong audience forms, missing
  token_type, nested JSON, URL controls, client-secret redaction, session wrappers,
  blocked live content, paused approvals, cancellation, and reviewer queue isolation.
- `python -S -m unittest discover -s tests -p test_core.py -q`: **14 passed**.
- OpenAPI and shared contract/example checks: no drift. `git diff --check`: clean.
- This is a selected integration regression suite, not the full project/M09 suite,
  a fresh-clone install, live provider test, external IdP test or browser-client test.
  The reviewer's complete set of 19 surviving mutants was not supplied or rerun.

The shared-contract check initially failed because the Windows test environment could
not write its default temporary directory. It passed after selecting the existing
workspace test-temporary directory; no application bypass was added.

New optional M04 interface: PendingApprovalStore.list_pending is implemented by SQLite.
ApprovalStore itself is unchanged so existing stores/fakes remain compatible. The reviewer
queue returns a typed 503 if an alternate store does not implement that extension.
M08 must regenerate the client for the new listPendingApprovals endpoint and typed 503s.
