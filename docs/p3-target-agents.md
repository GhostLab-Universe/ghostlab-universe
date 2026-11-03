# P3 external target agents: plain-Python interface first

Milestone P3 routes an external target agent through the frozen P1
`TargetAdapter` behind a controlled gateway. This document is the proof
log for that routing. Status of the first proof below: **self-play**.

## What self-play means here

Both sides of the interface are deterministic scripts in this proof: a
scripted agent proposes tool calls, and the checked-in world answers
them. There are zero live calls, zero model invocations, no streaming
and no invented model identifiers; scripted agents record
`model`/`provider`/`endpoint` as None. A run against a model-driven or
third-party agent is a new experiment, not a replay of this one.

## What was built (ghostlab side, new files only)

- `src/ghostlab/targets/external.py` -- the plain-Python
  `ExternalTarget` interface (mappings in/out, no GhostLab imports
  required), the `ExternalTargetAdapter` routing one agent through
  start/propose/result/stop, and the `ControlledGateway` (tool
  allowlist plus argument schema; denials end the attempt pre-dispatch).
- `src/ghostlab/targets/evidence.py` -- UI-neutral evidence with three
  structurally distinct outcomes and no shared pass/fail badge:
  - `agent_retest`: the agent ran fresh; only a completed, unstopped
    retest carries `checks_passed`.
  - `recorded_replay`: recorded requests re-executed with no agent;
    the verdict is `matches_recording`, and `target.agent_reran` is
    always False.
  - `boundary_enforcement`: the gateway denied a proposal; the verdict
    is the denial, and the denied call never touched the world.
- Every record keeps Explorer, Target and Verifier identities in
  separate sections: `explorer.{id,version}`,
  `target.{adapter_id,adapter_version,model,provider,endpoint,actions,close_reason}`
  and `verifier.{version}`.
- `tests/targets/test_p3_external_targets.py` -- the self-play proof:
  a defective scripted refund agent (`selfplay-refund-agent`
  `defective/1`, retries a timed-out refund with a new idempotency key)
  fails under `timeout_after_commit`, while the fixed agent
  (`selfplay-refund-agent` `fixed/1`, checks refund status first)
  passes under the same conditions. Rogue agents prove unknown-tool
  and bad-argument denials leave the world untouched, replay matches
  without running any agent, and agents cannot corrupt the world
  through the observation copies they receive.

P1 interfaces, the controller, the importer, the rule library, the
capsule and the UI are untouched: this milestone only adds files.

## External proof (ghostlab-ext-example side, separate repo)

- `reservation_agents.py` -- one intentionally defective reservation
  agent (`reservation-agent` `defective/1`: blind retry after
  `TimeoutAfterCommit`, double-books) plus one fixed version
  (`reservation-agent` `fixed/1`: reconciles commit-time `used` growth
  before any retry), both plain Python against `call_tool`.
- `test_agents.py` -- system-interpreter self-verification: the
  defective agent double-books under `timeout_after_commit` (task
  verdict failed from authoritative `observe()`), the fixed agent
  commits exactly once under the same fault (passed), versions are
  named in every record, and both pass the no-fault baseline.

## How to reproduce

- Ghostlab worktree: `uv sync --frozen --all-extras --group dev`,
  then `pytest tests/targets` (or bare `PYTHONPATH=src python -m
  unittest discover -s tests` for the stdlib-only subset).
- Ext-example repo: `python test_service.py`, then
  `python test_agents.py`.

## Recommendation for later: one framework integration (not implemented)

Host a model-driven target as a LangGraph node that emits its next
proposal through `ExternalTargetAdapter` and the same
`ControlledGateway`, keeping the explicit graph as the only
orchestration framework. Rationale: the settled baseline already runs
LangGraph with custom provider nodes and rejects a second agent
harness, so the target side should reuse that shape instead of adding
one. Gates before any untrusted target code runs: reuse the gateway
and evidence records unchanged, qualify one `ExecutionBackend` with
network/filesystem/resource/secrets/evidence boundary tests, keep
provider continuation state opaque, and record every model-driven run
as a new experiment with its exact model identifier.
