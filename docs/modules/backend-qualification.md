# Execution-backend qualification status

Honest status per backend. Nothing here is a "secure sandbox" claim.

| Backend | Kind | Qualification | Notes |
|---|---|---|---|
| `trusted-local` | In-process, checked-in adapters | Trusted built-in only | Existing behavior, explicitly labelled. Not isolation. |
| local reference (`ghostlab.backends.local`) | OS subprocess + worker protocol | **Unqualified** | Reports `filesystem: none`, host network posture via capabilities. Validates protocol/lifecycle only; never a sandbox. |
| docker (`ghostlab.backends.docker`) | Hardened container, host-gated | **Unqualified** (see evidence) | Reports `blocked-no-daemon` with no daemon; with a daemon, qualification exists only when recorded adversarial evidence is stored via `record_qualification` and matches server version + image ref. |

## What qualification requires (docker)

1. Daemon reachable and worker image built by the operator from
   `src/ghostlab/backends/Dockerfile.worker` (digest pinned).
2. Adversarial profile executed against the real daemon: host/sibling
   canaries unread, zero egress, breakout writes absent, fork/disk/memory
   bombs bounded, cancellation confirmed, forged worker output rejected.
3. The executed evidence recorded with `record_qualification`; any
   server-version or image change invalidates it back to `unqualified`.

Until all three hold on the target host, the docker backend must be
treated as unqualified and the controller refuses silent fallback to
in-process execution (unknown/unavailable backend = refusal, HTTP 503
at the API layer).

## Out of scope

Untrusted multi-tenant execution is OUT of this milestone. Reviewed
developer code only. MicroVM/stronger runtimes require separate host
evidence before any claim. Job output is untrusted data; verdicts come
from controller-side rules over authoritative observations, never from
adapter-reported claims alone.
