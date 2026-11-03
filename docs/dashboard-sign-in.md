# Dashboard refresh and sign-in

This branch starts from the dashboard that was running in `verify-final` at
`8a9d0de`. It does not include Muse's uncommitted isolated-backend work.

## What is connected

The dashboard still uses the existing campaign, approval, evidence and experiment
APIs. Environments now have separate setup, experiment, evidence and repair panels.
Simple scenario values have form controls; raw JSON is an advanced editor.
Connection diagnostics are inside the workspace menu, not the dashboard header.

Public GitHub inspection resolves a branch to a commit and tree, checks bounded
metadata and reports root setup filenames. It never installs or executes a repo.
It does not register an environment, grant private-repository access, or claim
that a successfully inspected repo is runnable. The current app still needs a
reviewed adapter and qualification. This ports the read-only inspector from
`ac9878b`, adapting it to this dashboard's authenticated API boundary.

## Enable GitHub and Google sign-in

Both are optional and disabled until configured. Use a separate OAuth application
for GhostLab sign-in, requesting identity only. Do not reuse a broad repository
automation token. Provider tokens stay on the server during the identity lookup
and are not persisted or sent to the browser.

1. Register a GitHub OAuth app and a Google OAuth web client in accounts you own.
2. Choose the exact browser origin, e.g. `http://localhost:5199`. Use that same
   hostname and port in your browser; `127.0.0.1` and `localhost` are different origins.
3. Register these callback URLs, changing only the origin if necessary:
   - `http://localhost:5199/v1/auth/github/callback`
   - `http://localhost:5199/v1/auth/google/callback`
4. Put the following values in the API process environment, using an ignored local
   secrets file or a secret manager. The CLI does not automatically load `.env`.
   Never use a `VITE_` prefix for a secret.
   - `GHOSTLAB_AUTH_ORIGIN`
   - `GHOSTLAB_GITHUB_CLIENT_ID`, `GHOSTLAB_GITHUB_CLIENT_SECRET`
   - `GHOSTLAB_GOOGLE_CLIENT_ID`, `GHOSTLAB_GOOGLE_CLIENT_SECRET`
   - `GHOSTLAB_AUTH_MEMBERS` (operator-maintained JSON, example below)
5. Start the API with `--auth oauth`. Keep the existing root, model authorization
   and budget arguments if switching an existing deployment. A no-paid-call check
   can use `python -m ghostlab.api --auth oauth --scripted --root artifacts/login-check`.
6. Start the frontend with its `/v1` proxy pointed at that API. Unauthenticated
   visitors see the sign-in page; protected API requests return 401.

Example member configuration (replace these example immutable IDs):

```json
{
  "github:123456": {"principal_id": "developer-alice", "project_id": "ghostlab-team"},
  "google:109876543210": {"principal_id": "developer-bob", "project_id": "ghostlab-team"}
}
```

GitHub's numeric account ID and Google's `sub` are the identity keys. Do not use
emails or mutable usernames. Obtain IDs from the provider's authenticated account
administration/identity information. Unknown identities are refused, not enrolled.
Mapping two identities to the same principal deliberately links those accounts;
use different principals for different people. Restart after changing membership.

OAuth login uses state bound to an HttpOnly browser cookie, PKCE, fixed provider
endpoints, bounded responses, a total deadline and server-side sessions. Sessions
expire after eight hours, are revoked on logout, and disappear on restart. Mutating
requests require the configured browser Origin. Redirects are not followed during
server-side token or identity requests. Login attempts and sessions are bounded.

## Limits that remain

- Provider registration and a real GitHub/Google login must still be verified by
  the operator. Mocked flow tests are not evidence of live provider configuration.
- This is a single-process loopback application, not a hosted authentication
  launch. `production_authenticated` remains false and hosted startup stays refused.
- All configured members currently receive the existing local operator capabilities,
  including self-approval. Review-only roles and separation of duties are not added.
- Sign-in is independent of private-repository authorization. Neither login grants
  private GitHub import, customer data access, or paid model-call authorization.
- The API's historic `configured_tokens` session mode now also carries operator-
  assigned OAuth sessions. `/v1/auth/status` distinguishes OAuth availability and
  current cookie authentication; this does not change world/capsule contracts.
- The source inspector recognizes root setup files only. Set a branch explicitly
  when the repository's default branch does not contain your application.

References: [GitHub OAuth web flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
and [Google web-server OAuth flow](https://developers.google.com/identity/protocols/oauth2/web-server).

## Browser verification findings

On a fresh preview database, the existing built-in refund qualification reports
`reset_correctness: fail` because the adapter accepts an empty fixture through
its dataclass defaults. Separately, the experiment operator registry starts empty:
`refund-world/1` is not registered there, so Prepare run refuses the saved config.
The reviewed-adapter list and persisted operator registry are distinct in this
base. This branch does not bypass either check or silently register/qualify code.
These backend onboarding gaps need reconciliation with Muse's integration work.

Public GitHub inspection was verified in the browser for the default branch and
`architecture-and-refund-core`. The latter resolved to `8a9d0de` and detected
`pyproject.toml` and `uv.lock`. No repository code or model was executed by inspection.
