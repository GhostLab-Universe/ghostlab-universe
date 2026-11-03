# T5 candidate lifecycle (Release 2 core, developer-supplied mode first)

Backend for controlled improvement: versioned candidate records, allowed-change
validation, multidimensional evaluation, and human-approval-bound promotion.
New package `src/ghostlab/candidates/` plus `tests/candidates/`,
`scripts/prove_candidates.py`, and this doc. No other directory is touched.

## Lifecycle

`proposed -> validated -> evaluated -> promotion_requested -> promoted`,
with `-> rejected` from any pre-promoted state. A rejected approval (or an
expired one) returns the candidate to `evaluated` for a fresh request; only a
completed single-use approval consumption marks `promoted`.

## Records (`candidates/records.py`, stdlib only)

- **Candidate**: `candidate_id` (content-addressed per project, identical
  proposes idempotent), `mode`, `parent` / `proposed` endpoints
  (`target_id`, `target_version`, `code_identity`, `config_digest`), exact
  `diff` (non-empty, no duplicate entries), `evidence_refs` (references
  only, never content), `status`, `rejection` (present exactly when
  rejected: code, message, by, at), `created_by`, `created_at`, digest.
- **Evaluation**: `original` failing case plus `regressions`, each with full
  parent and candidate attempts (status, tool calls, stop, denial, per-check
  verdicts); `completion`, `fault_coverage` (per-case faults fired per side),
  `usage` (cap plus per-side tool-call totals). There is deliberately **no
  top-level verdict field** -- unknown fields are rejected, so no aggregate
  badge can hide a failing dimension.
- **Promotion**: binds `candidate_id` to one `approval_id` and its
  `action_digest`; `pending -> completed | rejected | expired`.

All records are project-scoped, digest-verified on read, secret-scanned
(reusing the experiment-config secret markers), and size-bounded.

## Allowed-change validation (`candidates/allowlist.py`, stdlib only)

- **Developer-supplied** (wired): both endpoints must resolve in the operator
  registry for the candidate world; both versions must equal their registered
  manifest versions (no phantom versions); the endpoints must differ; the
  diff must carry exactly one `target` entry that exactly describes the
  parent-to-proposed step, plus only allowlisted `field` entries.
- **Generated-config** (bounded contract, no live model wired): target stays
  identical, `config_digest` must change, diff is allowlisted fields only.
- **Refusals**: `not_allowlisted` (unknown tunable), `escape_attempt`
  (paths reaching toward code, identity, transport, tools, prompts or
  secrets -- segments split on `.`, `-`, `_`), `generated_code_refused`
  (any `code`-kind diff, in both modes), plus registration / exactness /
  bounds codes. Generated-code execution stays refused structurally: the
  package has no code execution path.
- Default tunables: `run.max_tool_calls`, `run.max_wall_clock_ms`,
  `run.repetitions`, `agent.timeout_ms`, `agent.max_retries`, each bounded.

## Promotion and the approval flow (`candidates/service.py`)

`CandidateService` reuses `policy/approvals.py` by import (that module gained
only a read-only `get_request` lookup). Authority comes from the trusted session (`TrustedScope` /
project id), never from record bodies. The promotion approval action pins
`promotion_id`, `candidate_id`, `project_id`, both endpoint identities and
the evaluation digest, so any change needs a new approval and consumption
is single use.

Contract constraint: `ghostlab-contracts/1` admits only `apply_repair`,
`change_policy` and `execute_consequential_action` as approval kinds, so
promotions use `execute_consequential_action` with candidate identity in
the action body. If contracts later gain a `candidate_promotion` kind,
only `PROMOTION_ACTION_KIND` changes.

## Evaluation harness (`candidates/evaluate.py`, stdlib only)

Runs parent vs candidate from registry factories on fresh environment
instances per case (original + regressions) through the generic runner.
Usage counts tool calls only -- wall-clock time is never measured, so
results are deterministic (tested by double-run equality). Zero model
calls, no network.

Every recorded evaluation carries the required `world_version` and the
evaluated `endpoints` triple (parent/proposed target id, version, code
identity, plus config digest when the change is config-only);
`record_evaluation` refuses evaluations whose world or endpoints differ
from the candidate under test.

## Demo targets (`candidates/demo_targets.py`, stdlib only)

`demo-refund-naive@demo-refund-targets/1` refunds the full INR 2,500 twice
(double-spend bug); `demo-refund-fixed@demo-refund-targets/2` reads the
order then refunds once. Registered alongside -- never replacing -- the
checked-in adapters.

## Proof

- `python scripts/prove_candidates.py`: registers v1+v2, proposes v1->v2,
  validates, evaluates (parent fails `total_refund_within_payment` and
  `no_duplicate_full_refund`, candidate passes), promotes through a real
  SQLite-backed approval, and shows `not_allowlisted`, `escape_attempt`
  and `generated_code_refused` refusals. Exit 0/1/2.
- `pytest tests/candidates`: 43 tests covering records, allowlist,
  refusals, store isolation/transitions, harness dimensions and
  determinism, the approval-bound service lifecycle, evaluation
  endpoint/world binding, CAS-first promotion, expiry, and secret
  redaction.

## Limits and non-goals

- One registry world per service; both endpoints must be *currently*
  registered (no version history yet -- a replaced parent needs
  re-registration or a history extension).
- Promotion marks records only; no deployment or traffic switch happens
  here. API/web wiring is integration work, not this milestone.
- Generated mode is contract + validation only; no model is called and no
  model output is accepted anywhere in this package.
- The service module needs the contracts extra (pydantic); the record,
  validation, store, harness and demo modules are dependency-free.
