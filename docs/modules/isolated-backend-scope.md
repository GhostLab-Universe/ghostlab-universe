# Isolated execution backend: §1 scope freeze (integration owner record)

Base: `feature/byoe-config-experiments` (post-testing-pipeline HEAD; this doc
written while T-integration is in flight, describing the integrated state).

## Verified starting point (all checked, not assumed)

- Execution path: `controller.execute_run` → in-process adapter instance
  (`EnvRegistry` allowlist + code digest) → world object in the controller
  process. Same for discovery (`describe()`) and qualification (7 checks).
- Isolation is declared `in-process-checked-in-local` (descriptive, not a
  sandbox claim). No `ExecutionBackend` interface exists anywhere in the
  tree (only the "not a sandbox" disclaimer in `targets/gateway.py`).
- 5ea1a4d claims re-verified: reviewed classes imported into host process:
  TRUE. No qualified boundary for arbitrary imported code: TRUE.
- Trust posture today: reviewed, checked-in, operator-registered code only;
  allowlist + digest binding + project scoping + budgets + evidence
  ownership. Correct for trusted code; NOT a boundary for untrusted code.

## Actual platform (verified 2026-11-03)

- Dev machine: Windows 11 Home (Build 26300). No cloud spending authorized.
- Docker CLI present (`Docker/resources/bin/docker.exe`) but the daemon is
  NOT running (`dockerDesktopLinuxEngine` pipe absent).
- WSL2 present, default distro Ubuntu. Docker inside WSL: not yet checked.

## Proposed trust boundary

```
GhostLab controller (API, orchestrator, policy, evidence, credentials)
  → ExecutionBackend interface (versioned, structured protocol)
    → isolated job(s): target agent + test environment + faults
  → bounded event/result channel (untrusted input, validated)
  → controller-side verification + project-scoped storage
```

Controller never imports adapter code for registered/qualified/executed
envs once a backend is selected; manifest parsing stays data-only;
discovery + qualification also move inside the backend. No runtime socket,
no control-plane credentials, no host mounts inside jobs. No silent
fallback to in-process execution (explicit backend selection; in-process
remains only as explicitly labeled trusted-local mode).

## Release strategy (host-gated, honest)

1. Threat model + versioned `ExecutionBackend` interface + structured
   protocol (seq numbers, job identity, size/depth limits; no pickle/eval).
2. Reference local-process backend implementing the interface, LABELED
   unqualified-for-isolation (validates protocol/lifecycle/evidence
   handling only — never presented as a sandbox).
3. Full adversarial qualification SUITE (canary reads, metadata endpoints,
   breakout writes/traversal, fork/CPU/mem/disk bombs, malformed output,
   cancel survival, ID forgery) — must demonstrably FAIL against the
   reference backend where enforcement is absent.
4. Controller integration behind explicit selection + UI status surface.
5. Linux container backend + adversarial qualification EVIDENCE — requires
   a running daemon (start Docker Desktop or dockerd in WSL2). If no
   runtime can be activated on this machine, step 5 is reported blocked
   with exact host requirements; nothing is claimed from config files.

Categories: trusted built-in (exists) → isolated reviewed-code (this
milestone, host permitting) → untrusted multi-tenant (explicitly OUT;
needs a stronger runtime + independent qualification).

## Ownership and dependencies

- New: `src/ghostlab/backends/` (interface, protocol, local reference,
  container backend), `tests/test_backends_*.py`, UI status additions.
- Touches: controller dispatch (backend selection), service routes
  (backend status), docs. No changes to worlds, checks, or evidence
  formats. No new formats.
- Depends on: testing-pipeline integration committed first (driver
  interface is what jobs will execute). Separate branch
  `feature/isolated-backend`. Other sessions' live processes
  (trading/vite/API) are protected state — untouched.
- Model access: brokered, keys never in jobs; NO live inference is
  authorized — broker validated offline only.

## Supported now vs deferred

Now: interface + protocol + lifecycle + local reference (unqualified) +
adversarial suite + controller wiring + reviewed-code path design.
Deferred: container enforcement evidence (host-gated), image/package
intake pipeline, untrusted multi-tenant, hosted runners, GPU jobs.
