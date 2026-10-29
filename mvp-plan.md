# MVP plan

Build one complete refund failure demonstration before adding more worlds.

| Step | Deliverable | Acceptance criterion |
| --- | --- | --- |
| 1 | Experiment and capsule schemas | Baseline, fault schedule, trace events and invariant results have versioned fields |
| 2 | Refund world and adapters | Baseline refunds exactly INR 2,500 once; unauthorized requests cannot mutate the ledger |
| 3 | Fault injection | Timeout occurs after commit; vulnerable retry produces an independently detected duplicate refund |
| 4 | Replay | The saved capsule reproduces the same recorded actions and final state without a model call |
| 5 | Reality Diff | Interface shows the first divergent event, both ledger totals and the violated invariant |
| 6 | Adaptive planner | Model proposes schema-valid experiments within a fixed budget and receives verifier feedback |
| 7 | Repair verification | Repaired adapter passes the original capsule and nearby retry and authorization cases |
| 8 | Demo package | README includes real run instructions, a recorded example and measured results |

## Suggested two-person split

Agree on ownership together. One person can own the world, runner and verifier; the other can own the interface, provider adapter and capsule display. Review each other's pull requests. Both tracks depend on the shared experiment schema from step 1.

## Evidence to collect

Measure discovered seeded failures, executions required, replay success and false positives. Compare random injection, one-pass generated scenarios and adaptive exploration using equal execution budgets and the same world version. Report sample sizes and all runs, including failures. Do not claim adaptive exploration is better before measuring it.

## Demo sequence

Run the baseline, introduce a timeout after commit, inspect the duplicate ledger entry, replay the failure, apply the reviewed idempotency repair and rerun the regression cases. Label any pre-recorded model output clearly.

## Deferred work

World generation, a general adapter framework, a second world and a shared registry are follow-up work. Keep the first demo reproducible without cloud execution.
