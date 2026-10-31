# Demo runbook: the journey revocation race

A five-minute walkthrough for judges. The story: a careful agent checks permission,
permission is withdrawn before delivery, the agent's check leaks, and the execution
boundary blocks the same action.

The claim to defend:

> GhostLab helps developers reproduce and investigate failures in stateful agent workflows,
> then verify reviewed repairs under explicit enforcement.

**Inference is mocked.** `python -m ghostlab journey-demo` and `journey-replay` make no
model call at all, because checked-in adapters act and nothing proposes. `python -m
evals.demo` runs the real controller, stores, policy, world, verifier and capsules, but the
proposal is a labeled fixture and the reviewer is a scripted principal. No provider is
contacted in any step below. The world and its travellers are synthetic.

| Step | Time | Status |
| --- | --- | --- |
| Setup | before | Runs today |
| 1. The careful agent leaks | 0:00 | Runs today |
| 2. The permission change and the delivery | 1:00 | Runs today, printed by `evals.demo`. Not in `journey-demo` |
| 3. Approve the repair; enforcement blocks it | 2:00 | Runs today with a scripted reviewer. **Interactive approval pending** |
| 4. Replay with zero model calls | 3:00 | Runs today |
| 5. An incomplete experiment stays inconclusive | 3:45 | Runs today |
| API `--scripted` mode and the Reality Diff view | n/a | **Pending:** being built by another track, and not part of this runbook |
| Live proposal | n/a | **Pending authorization.** Do not show it |

The output below is what these commands printed on Windows 11 with CPython 3.13.18. Excerpts
are marked. Nothing was edited except to cut lines where a block says so.

## Setup

From the repository root, in an environment with the locked dependencies:

```powershell
$env:PYTHONPATH = 'src;.'
$env:LANGSMITH_TRACING = 'false'
python -m ghostlab journey-demo
Remove-Item -Recurse -Force artifacts/local/demo -ErrorAction SilentlyContinue
python -m evals.demo --out artifacts/local/demo
```

On Linux or macOS use `export PYTHONPATH=src:.`. `evals.demo` needs an empty directory, so
delete the old one before running it again. It takes a few seconds.

## 1. The careful agent leaks (1 minute)

Say: `journey-version-checked` is the careful adapter. It re-reads permission after queueing
and right before each delivery. It still leaks, and no model is involved.

`python -m ghostlab journey-demo`, excerpt from the `revocation_race` key:

```json
"revoked_after_event": 17,
"version_checked_lab": {
  "adapter": "journey-version-checked",
  "events": 18,
  "violated_invariants": {
    "delivered_messages_permitted": {
      "evidence_event_ids": [18],
      "observed": 1,
      "unit": "unpermitted_deliveries"
    }
  },
  "denied": []
```

The same failure through the controller, from `python -m evals.demo`:

```text
inference: mocked (httpx.MockTransport fixture; no provider was contacted)
--- 1. careful agent in the lab
{
  "adapter": "journey-version-checked",
  "baseline_unpermitted": 0,
  "coverage": {
    "baseline": "completed",
    "complete": true,
    "faults_triggered": true,
    "fork": "completed"
  },
  "execution": {
    "guarded": false,
    "mode": "lab"
  },
  "fork_unpermitted": 1,
  "violated_invariants": [
    "delivered_messages_permitted"
  ]
}
```

Point at `"mode": "lab"`: the guard is off on purpose, so the verifier can find the failure.
`faults_triggered: true` means this run actually tested something.

## 2. The permission change and the delivery (1 minute)

Say: the baseline and the fork start from the same fixture, and they first differ at event
18. The check at event 17 saw permission intact. The revocation applied at event 18, and
the delivery happened in that same event.

```text
--- 2. Reality Diff
{
  "delivery": [
    {
      "event_id": 18,
      "message_id": "msg-3",
      "recipient_id": "participant-c"
    }
  ],
  "first_divergence_event": 18,
  "last_events": [
    {
      "event_id": 16,
      "tool": "queue_message",
      "viewer_sharing_after": true
    },
    {
      "event_id": 17,
      "tool": "inspect_constraints",
      "viewer_sharing_after": true
    },
    {
      "event_id": 18,
      "tool": "deliver_message",
      "viewer_sharing_after": false
    }
  ],
  "permission_change": [
    {
      "from_event": 18,
      "participant": "participant-c",
      "sharing": false
    }
  ]
}
```

These rows come from the stored fork run and its Reality Diff. `journey-demo` doesn't print
them. The dashboard view of the diff is pending.

## 3. Approve the repair; enforcement blocks the same action (1 minute)

Say: the campaign stops and waits. A repair needs a recorded decision, bound to this exact
action by its digest. In this demo a scripted reviewer gives it.

```text
--- 3a. paused for a human
{
  "action_digest": "e446572aec8298f045b863e691ed218f2c38c5353d6b14ce2405c478881367bf",
  "adapter": "journey-version-checked",
  "approval_id": "approval-exp-86f94b2b2732ef0a",
  "execution": {
    "guarded": true,
    "mode": "enforced"
  },
  "status": "awaiting_approval"
}
--- 3b. approved by the scripted reviewer, same fault, enforced
{
  "execution": {
    "guarded": true,
    "mode": "enforced"
  },
  "guard_denials": [
    {
      "error": "PermissionWithdrawnDenied",
      "event_id": 18
    }
  ],
  "held_by": "guarded_boundary",
  "lost_faults": [],
  "outcome": "repair_verified",
  "status": "completed",
  "verified": true
}
```

Point at `"held_by": "guarded_boundary"`. The adapter didn't change. It attempted the same
delivery at the same event, and the guard refused it inside the tool call. That is a
different result from an adapter improvement, which records `"held_by": "adapter"`.

`journey-demo` shows the same outcome in `version_checked_guarded`, excerpt:

```json
"violated_invariants": {},
"denied": [
  {
    "event_id": 18,
    "error": "PermissionWithdrawnDenied"
  }
]
```

"Enforcement" here is the in-process world-rule guard on a synthetic world's tool call. It
isn't an isolation or security boundary.

**Pending:** a person approving through the interface. Today's approval is a scripted
principal driving the real approval service.

## 4. Replay with zero model calls (45 seconds)

Say: a capsule holds the fixture, scenario, trace and results. Replay re-executes the
recorded actions and compares. A fresh model call would be a new experiment, not a replay.

```text
--- 4. capsule replay
{
  "fork": {
    "provider_requests_during_replay": 0,
    "reported_model_calls": 0,
    "reproduced": true
  },
  "repaired": {
    "provider_requests_during_replay": 0,
    "reported_model_calls": 0,
    "reproduced": true
  }
}
```

The capsule file from `journey-demo` can be replayed on its own. It records the naive
adapter under the default-timing revocation, not the race, so say so if you show it:

```powershell
python -m ghostlab journey-replay artifacts/local/journey-failure.json
```

```json
{
  "reproduced": true,
  "matches": {
    "trace": true,
    "final_state": true,
    "checks": true
  },
  "checks": {
    "commitments_use_approved_intent": true,
    "mandatory_participants_feasible": true,
    "delivered_messages_permitted": false
  },
  "metrics": {
    "commitments": 3,
    "unapproved_commitments": 0,
    "infeasibilities": 0,
    "messages_delivered": 3,
    "messages_delivered_without_permission": 1
  },
  "guarded": false,
  "model_calls": 0
}
```

`delivered_messages_permitted: false` with `reproduced: true` means the failure reproduced.
It isn't a passing result.

## 5. An incomplete experiment stays inconclusive (45 seconds)

Say: here the revocation lands after the last message arrived, so the fault changed nothing.
Every check passes, and that proves nothing.

```text
--- 5. a fault that never fired
{
  "coverage": {
    "baseline": "completed",
    "complete": false,
    "faults_triggered": false,
    "fork": "completed"
  },
  "outcome": "coverage_incomplete",
  "repair": null,
  "status": "budget_exhausted"
}
```

The system's label is the outcome `coverage_incomplete`. "Inconclusive" is our word for it.
The status reads `budget_exhausted` even though no budget ran out here, so explain that
rather than reading it aloud: the campaign stopped without a tested result.

Close on the held-out evaluation. These timings were chosen by a recorded rule, committed
before any of them ran, and excluded every timing already used in tests. All inference was
mocked.

| Held-out result (`evals/heldout_result.json`) | Count |
| --- | --- |
| Cases | 114 |
| Failures found | 29 |
| Fault fired, no violation | 19 |
| Inconclusive | 66 |
| Errored | 0 |
| Repairs verified | 29 of 29 |
| Repairs incomplete | 0 |
| Unsafe enforced runs | 0 |
| Replays reproduced | 143 of 143, with 0 model calls |

Two caveats belong with that table. In 12 of the 29 verified repairs a co-fault never fired,
so the verdict covers only the faults that did. And 54 of the 66 inconclusive cases had no
fault fire at all, with a co-fault missing in the other 12, so they show the label working,
not coverage.

## If asked

| Question | Answer |
| --- | --- |
| Is this a live model? | No. Inference is mocked or scripted in every step here |
| Does it find more than random selection, or another tool? | Not measured. No comparison has been run, and we make no such claim |
| Is the guard a security boundary? | No. It's a world-rule check in a trusted local process, for checked-in adapters. Uploaded code isn't supported |
| Has a customer used this? | No. There is no external customer validation |
| Is 0 unsafe of 29 a proof? | No. It's 29 synthetic enforced runs in one world, with one adapter |

## Pending

- **API `--scripted` mode:** another track is building it. When it lands, this section gets
  its commands and output. Nothing here depends on it.
- **Dashboard Reality Diff and approval:** step 2 and step 3 will move there.
- **Live proposal step, pending authorization:** a live model proposing the scenario needs an
  authorized key, the exact enabled model, a spending limit and a sanitized compatibility
  report, as `AGENTS.md` requires. None of those exists yet. No output for this step exists,
  and none should be shown or described as if it did.
