# Independent security hardening

1 November 2026. Branch `codex/security-hardening`, based on M07 `c19fe9d`.
This is a separate worktree. It does not change Claude's implementation branches,
locks, engine, world manifests or M09 release switch.

## Authentication integration

`ghostlab.api.introspection.IntrospectionSessions` implements the existing
`SessionAdapter` protocol using [OAuth token introspection (RFC 7662)](https://www.rfc-editor.org/rfc/rfc7662).
The authorization server validates opaque access tokens. This application requires
`active: true`, issuer, audience, expiry, subject and the `ghostlab` scope; it also
checks any `nbf` and `iat`. Those fields are an explicit stricter deployment profile,
not a claim that RFC 7662 requires every field.

The server maps the issuer's subject to a principal and one project. Token/body
project and role claims do not grant membership. Membership configuration is an
operator-controlled allowlist; changing it requires replacing/restarting the adapter.
Revocation at the issuer is checked on every request. Introspection has no cache or
retries, bounded response size, bounded concurrency, verified HTTPS, no redirects and
no environment proxy inheritance. Authentication runs outside the ASGI event loop.

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
introspection adapter paired with the legacy approval service. The explicitly labelled
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

The policy for this build is **synthetic data only**. Customer, personal and unclassified
content are refused by `RetentionDecision.require`. Each decision names exact provider,
endpoint and model, a reviewer, a policy source and a timezone-aware review date; it
expires after 30 days. Account controls must be explicitly verified. This check neither
authorizes paid calls nor certifies a deployment. In `configure_introspection`, a live
service additionally needs this decision; the adapter factory checks it against the
actual provider configuration. Existing M01/M05 live checks still apply.

[Baseten's security practices](https://www.baseten.co/security-practices/) state a zero
content-retention posture for Model APIs, subject to listed configuration and product
exceptions. Confirm the account/product and avoid optional payload logging before
setting `account_controls_verified`. This is a published posture, not an account audit.

[Meta's Responses schema](https://dev.meta.ai/docs/api-reference/responses/schemas)
documents `store` as true by default. [Meta's retention guidance](https://dev.meta.ai/help/policies-and-privacy/zero-data-retention)
describes ZDR as an account capability. The existing adapter uses stateful continuation;
we do not change it or promise that `store: false` alone means zero retention. Direct
Meta live use stays blocked until the actual account controls and continuation choice
have been reviewed and recorded. No account setting is changed in this work.

Local memory, checkpoints, artifacts and telemetry retain their existing policies.
This branch does not claim to schedule deletion or silently delete evidence. Use M09's
retention/pruning process and agree the demo evidence retention window before live use.

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

Recorded local results, Windows, Python 3.13.2, existing locked environment:

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
