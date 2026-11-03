# Isolated backend: threat model (integration owner record)

Scope: reviewed developer test environments + target agents executed as
isolated jobs. Trusted built-in in-process execution is unchanged and
explicitly labeled. Untrusted multi-tenant execution is OUT of this
milestone.

## Assets (controller side, never in jobs)

A1 provider credentials and control-plane secrets. A2 authoritative
evidence store + run records. A3 other projects' inputs/outputs/state.
A4 host, runtime socket, cloud credentials, home repo. A5 aggregate
budgets, policy ceilings, job identities.

## Actors

- Operator: installs GhostLab, approves reviewed packages/images, reads
  evidence. Trusted, but mistakes must fail closed (unknown backend,
  unavailable backend, malformed output => refusal, never fallback).
- Developer: submits reviewed packages + configs for THEIR project.
  Code is reviewed yet buggy; may be actively curious. Must not reach
  A1-A5 or other projects.
- Job payload: arbitrary code paths INSIDE the approved package (bugs,
  dependency confusion, malicious snippets that slipped review). Treated
  as hostile within the job boundary; its OUTPUT is untrusted data.

## Threats (each maps to an adversarial check)

- T-host-read: read host canary / repo / home / cloud creds.
- T-sibling: read another job's files, attach to its processes, reuse
  its state, cancel/inspect its jobs; forge job IDs or result paths.
- T-egress: metadata/credential endpoints, control-plane services,
  arbitrary internet; DNS rebinding to smuggle destinations.
- T-escape-write: write outside the job workspace; symlink/traversal
  through inputs, outputs, or archives; overwrite existing evidence.
- T-exhaust: fork/CPU/memory/disk/output exhaustion harming the host or
  starving siblings; surviving cancellation via child processes.
- T-output: oversized/malformed events crashing or confusing the
  controller; fabricated state/traces presented as truth.
- T-supply: traversal/absolute-path/unsafe-link/expansion-bomb archives;
  dependency install during test runs; unapproved image substitution.
- T-channel: provider keys in jobs; general HTTP proxy abuse; silent
  model substitution; unaccounted retries.

## Explicit assumptions (rely on, do not re-prove here)

Host kernel + container runtime (if present) are intact; the operator
approves only reviewed packages and keeps the daemon socket off jobs;
reviewed code is non-hostile by policy but buggy by default. If any
assumption fails, the backend is disqualified (not silently weakened).

## Non-goals (do not claim)

Kernel exploits, microarchitectural attacks, malicious-operator defense,
multi-tenant untrusted containment, production-fidelity of simulated
worlds, or truthfulness of adapter-reported state (verified only via
authoritative observations + reviewed rules).

## Enforcement map (brief -> mechanism)

Filesystem/process boundary: non-root, no privileged, own namespaces,
no socket/devices, dropped caps + no-new-privileges, read-only base,
bounded rw workspace, no repo/home/cred mounts. Network: default none;
model access only via controller broker (keys outside jobs, authorized
provider/model/op, external accounting). Resources: CPU/mem/pids/disk/
wall/output caps. Integrity: controller-assigned job ids + receipt
order, strict message validation, project-scoped storage, observed-vs-
claimed separation, termination/truncation/missing-evidence preserved.
Lifecycle: prepared/starting/running/completed/failed/cancelled/
timed_out; cancel terminates descendants with bounded grace +
escalation + confirmation; reconcile sweeps orphans after restart.
