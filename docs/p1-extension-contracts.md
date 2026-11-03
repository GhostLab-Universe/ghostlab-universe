# P1 extension contracts (frozen): worlds, targets, registry, runner

Interface version ``ghostlab-extensions/1``. P2-P5 implement against this
document. The code is standard-library only: ``src/ghostlab/extensions/``
(interfaces, registry, runner) and ``src/ghostlab/adapters/`` (checked-in
refund package proving them). The controller seam is
``src/ghostlab/orchestration/extension_driver.py``.

## Environment adapter: the world side

Implement ``EnvironmentAdapter`` for one world version. One instance serves
one run: ``reset`` starts it, ``execute``/``observe`` serve it, ``close``
ends it. Parallel runs use separate instances.

| Method | Contract |
| --- | --- |
| ``describe`` | Tools (name, effect, parameters), faults (ids, params, trigger points), invariants, fixtures, capabilities, versions, source package. |
| ``named_fixture`` | Fixture snapshot shipped under a name. Unknown names raise ``KeyError``. |
| ``reset(fixture, scenario, *, actor_id)`` | Clean run from a fixture snapshot and fault schedule. ``actor_id`` is the controller-bound identity. Invalid input raises ``ValueError``. |
| ``execute(action)`` | Apply exactly one validated action. Every applied action appends one trace event, including world errors. Only a budget refusal raises (``ActionBudgetExceeded``); malformed actions raise before touching state. |
| ``observe`` | Authoritative trace, state and call count. Private copies. |
| ``evaluate(*, trace, state)`` | Deterministic invariant checks from evidence. Pure: same input, same checks, live adapter or fresh one. Failed checks cite events. |
| ``faults_fired(scenario, trace)`` | Fault ids that demonstrably took effect. A scheduled fault that never fired is not reported. |
| ``close`` | End the run. Idempotent; the runner always calls it. |
| ``snapshot`` / ``restore`` | OPTIONAL. Raise ``UnsupportedOperation`` when absent. The controller and runner never assume them; ``capabilities["snapshot_restore"]`` declares support. |

The controller validates every action (allowlist, schema, budget) before
dispatch. The adapter owns only the world's own checks: identity, existence,
ceilings.

## Target adapter: the tested agent side

Implement ``TargetAdapter`` for one adapter id. One instance serves one run:
``start`` begins it, ``propose``/``notify_result`` alternate until the target
finishes (``propose`` returns ``None``), and ``close`` always ends it with
``finished``, ``denied``, ``budget_exhausted``, ``error`` or the
controller's stop reason. ``info`` records adapter identity, model/provider
configuration (``None`` for deterministic scripts) and every visible action
with its outcome status. Proposals are untrusted data.

## Runner guarantees

``run_attempt(env, target, task, *, authorize, cap, should_stop,
cooperative_stop)`` owns only the loop. Per iteration: stop check (when
cooperative), propose, ``authorize`` (a non-``None`` return ends the attempt
as denied without touching the world), cap check (past ``cap`` ends as
``budget_exhausted``), ``execute``, ``notify_result``. Denial wins over an
exhausted cap, mirroring authorized world calls. ``first_divergence`` and
``replay_trace`` (recorded-action re-execution, no new proposals) are shared
world-agnostic helpers.

## Registry rules

``AdapterRegistry`` holds factories handed to it by reviewed local code. Only
``checked_in`` manifests register; duplicates need explicit ``replace=True``;
unknown ids fail closed. There is deliberately no import-by-name, path
loading, install or exec API: strings from proposals, capsules or requests
never become code, and the browser never runs uploaded Python.

## Controller seam: no domain conditions

``ExtensionDriver`` delegates every world answer to its ``ExtensionBinding``
(see the field contracts in ``extension_driver.py``). Adding a world means
registering a binding; the controller and driver do not change.
``execute_with_runner`` wires factories, reset, task, authorization and run
assembly to the generic runner for worlds without a legacy engine seam.

## Authoring checklist for a new world

1. Implement both adapters in a reviewed local package; keep them
   dependency-free. 2. Register factories plus manifests on an explicit
   registry. 3. Write an ``ExtensionBinding`` (models, scopes, runs,
   diff/findings assembly, repair answers, capsule export/replay).
4. Prove parity: identical traces, states, checks, diffs, capsule bytes and
   replay against the world's direct path, plus a no-snapshot run.

## Refund migration notes

``refund-world/1`` runs behind these interfaces today. ``execute_run`` drives
the checked-in adapters through the generic runner; ``RefundDriver`` is the
generic driver with ``REFUND_BINDING``. Evidence, diffs, findings, capsule
bytes and replay are identical to the direct ``ghostlab.core`` path (see
``tests/orchestration/test_p1_refund_parity.py`` and ``tests/extensions/``).
Contracts, capsule formats and the journey world are unchanged. Out of scope
here: importer, rule library, capsule changes, UI, live calls, streaming.