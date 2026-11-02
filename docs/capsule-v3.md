# Failure Capsule v3 for external environments

Contract set ``ghostlab-contracts/1.2`` (additive). v3 is a versioned capsule
evolution so external environments (P5) can record what v2 cannot hold
without breaking v1 and v2 readers. An independent critic reviews this contract
before P5 builds on it.

## Versioning decision

A v3 payload is exactly the v2 payload plus one OPTIONAL field,
``provenance``. ``None`` means plain v2 semantics: a self-contained recorded
capsule. All v2 field names, meanings and bounds are unchanged.

Why a new version instead of optional v1 and v2 fields: both v1 and v2 readers
require exact field sets, so any in-envelope addition breaks them. A new
``schema_version`` keeps every old capsule byte-valid under its own reader
while v3 carries the companion metadata P5 needs: environment and adapter
versions, fixture name and digest, rule versions, fault firing order, world
config, external dependencies and truncation flags.

Why the metadata lives in the envelope instead of a sidecar: one file keeps
one checksum over evidence and provenance together. A sidecar would need its
own reference integrity story for no replay benefit, since P5 replay must
resolve provenance against the adapter registry either way.


## The provenance block

``CapsuleProvenance`` is optional companion metadata. Every field except
``replay_kind`` defaults to absent or empty, so a minimal block records only
that the capsule is a plain recorded replay. The fields:

- ``replay_kind`` (``recorded`` or ``retest``). ``recorded`` means evidence
  from recorded-action replay: deterministic, no new inference. ``retest``
  means evidence from a fresh adapter or agent execution, which is a new
  experiment by definition, never a replay-equivalence claim.
- ``environment_id`` and ``environment_version``: the P1 environment that
  recorded the run, for example ``refund-environment`` at
  ``refund-environment/1``. Shaped like all P1 registry ids.
- ``fixture_name`` and ``fixture_sha256``: the P1 fixture name (for example
  ``synthetic-inr-2500``) plus the canonical digest of the embedded fixture.
  The contract recomputes the digest, so a wrong one fails validation.
- ``rule_versions`` maps a rule id to its version, for example each refund
  invariant to the engine verifier version.
- ``faults_fired`` lists the scheduled fault ids in firing order, matching
  the P1 ``faults_fired`` detection. Scheduled but unfired faults stay out.
- ``config`` is a scalar world-config snapshot. No nested objects.
- ``external_dependencies`` names resources outside the capsule.
  Names plus pinned versions or digests only. No URIs, credentials or live
  handles. A non-empty list means the capsule is NOT reproducible from its
  own bytes.
- ``truncated`` marks cut-short evidence. Verdicts from it are incomplete
  by construction.
- ``retest_adapter_id`` and ``retest_adapter_version`` name the retesting
  adapter. Required exactly for ``retest``. They may name an adapter outside
  the world pack, since a retest can come from an external agent.

The contract checks shapes, same-world consistency, the trace budget, the
fixture digest and the fault schedule. P5 replay resolves the environment,
fixture and adapter ids against the adapter registry and fails closed on
unknown ids.

## Compatibility and conversion rules

Every conversion validates the incoming checksum BEFORE converting. A
mismatch raises and is never replaced with a fresh-valid checksum.

- v2 to v3 upgrades a checksum-verified v2 capsule; provenance stays
  absent (``None``, plain v2 semantics). The v3 checksum is fresh.
  v2 evidence round-trips byte-identical.
- v3 to v2 downgrades only a provenance-free v3 capsule. A present block
  would be silently lost, so the downgrade refuses with an explicit error
  naming the replay kind, the external count and the truncation flag.
- v1 to v3 and back compose through v2. Each stage checks its checksum.

v2 to v3 to v2 restores the exact v2 bytes, including the checksum. v1 to v2
to v3 to v2 to v1 restores the exact v1 bytes. Both round trips are tested.

## Replay rules for P5

- ``recorded`` with no external dependencies and ``truncated: false`` replays
  through the recorded-action path after a v3 to v2 downgrade.
- ``retest`` capsules are new experiments. They never claim replay
  equivalence with the recorded run.
- Capsules with external dependencies are not reproducible from their own
  bytes. Replayers must refuse recorded replay and name what is missing.
- ``truncated`` capsules carry incomplete evidence by construction.

## Frozen P1 identifiers used

v3 aligns with the frozen P1 names and changes none of them: world versions
such as ``refund-world/1``, the environment id ``refund-environment`` at
``refund-environment/1``, target ids such as ``vulnerable`` and ``repaired``
at ``refund-adapters/1``, the fixture name ``synthetic-inr-2500``, the fault
id ``timeout_after_commit`` and verifier versions such as
``refund-verifier-1``. World versions stay the reviewed closed set: adding a
world is an M00 change, and P5 resolves external ids against the adapter
registry instead of inventing free-form strings.


## What was rejected and why

- In-envelope optional fields on v1 or v2: rejected, since exact-match
  readers in the field would break on any addition.
- A sidecar provenance file: rejected, since it splits the checksum and
  adds reference-integrity work for no replay benefit.
- A ``latest`` or unversioned capsule: rejected, since silent shape drift
  breaks the byte-for-byte replay promise.
- New world versions for old packs: rejected as out of scope. Implemented
  packs are frozen, so v3 reuses the recorded-action declaration.

## Evidence and review

The published v3 examples are generated by executing the synthetic worlds,
like the v1 and v2 examples. ``tests/contracts/test_capsule_v3.py`` pins the
frozen v1 and v2 artifacts byte-for-byte, replays every old capsule,
round-trips v2 to v3 and back exactly, and rejects tampered, mismatched and
over-budget capsules. This contract is reviewed independently before P5
builds on it. No live calls, no streaming, no model identifiers and no
secrets are involved.

## Reservation-world/1 envelopes (non-pack worlds, milestone T4)

This section is the explicit M00 follow-up the envelope boundary anticipated:
enveloping ``reservation-world/1``, the one non-pack world in the tree (the
vendored external reservation service behind its checked-in adapter). No
second non-pack world exists yet, so only reservation is enveloped; the
generalization recipe below keeps the next one mechanical.

### Where it lives

``ghostlab.contracts.world_reservation`` holds a reviewed reservation manifest
plus the envelope types. ``CapsuleV1``, ``CapsuleV2`` and ``CapsuleV3`` are
byte-untouched: reservation envelopes reuse the v3 payload layout, the v3
``CapsuleProvenance`` rules and the canonical capsule checksum, but validate
names against the reservation manifest instead of the pack registry.

The manifest mirrors the reviewed adapter exactly -- the four tool schemas,
effects and descriptions, the three fault ids with their P1 trigger points,
the four invariants, the four fixtures, the environment id and version, the
source string and the ``VENDOR_SHA256`` pin (lowercased into ``Sha256Hex``
shape). ``tests/contracts/test_capsule_v3_reservation.py`` cross-checks every
mirror, the way pack manifests are cross-checked against the real core.

### Deliberate differences from pack worlds

- Open adapter set. Reservation is a bring-your-own-agent world: external
  agent ids such as ``demo-booker`` at ``demo/1`` cannot be enumerated in
  advance, so only their shape is checked. This is the policy
  ``CapsuleProvenance`` already applies to ``retest_adapter_id``. Pack
  worlds keep their closed adapter sets.
- Parameterless faults, no timing. The world takes boolean toggles only, so
  the envelope requires empty fault parameter lists. A non-default
  ``after_event`` validates in the envelope but is refused by the evidence
  mapping, exactly like the refund v1 mapping refuses custom fault timing.
- The vendored service is always declared. Every reservation capsule names
  ``reservation-service`` at ``ghostlab-ext-example@79684eb`` with the pinned
  service digest first in ``external_dependencies``. The service is reviewed
  in-tree code, but it is developed outside this tree and re-vendored by
  hash, so omitting the pin would overclaim self-containment. By the P5 rule
  the envelope itself therefore does not claim self-contained recorded
  replay; ``replay_recorded`` still reproduces against byte-identical service
  code (proven in tests), and a replayer may verify the pinned digest before
  replaying. Recorded-replay policy for pinned vendored bytes belongs to the
  replay track.

### Compatibility and conversion rules

- Reservation capsules are v3-only. There is no pack v2 capsule for a
  non-pack world, so no v2 downgrade exists; the pack readers (``CapsuleV2``,
  ``CapsuleV3``, the v1 loader) reject reservation capsules, and the
  reservation reader rejects every pack capsule. Both directions are tested.
- Evidence converts losslessly. ``reservation_evidence_to_v3`` seals executed
  evidence (fixture snapshot, toggle scenario, run evidence, detection-based
  ``faults_fired``) with a fresh checksum; ``reservation_capsule_to_evidence``
  verifies the checksum BEFORE converting and never replaces a mismatch. The
  per-event actor the adapter records is stripped on sealing (after proving
  it equals the capsule actor) and restored on opening, so
  evidence-capsule-evidence is exact, including the checksum on resealing.
- No new contract version. The payload layout, provenance rules and checksum
  algorithm are v3's unchanged, so no version is minted and the registry,
  the contract set version and every published schema stay byte-identical.

### What was rejected and why

- A ``reservation-world/1`` entry in ``PACKS``: rejected, since the frozen
  v1/v2 readers and published v2 schema pin the closed pack set; extending
  it would rewrite frozen artifacts.
- Widening ``CapsuleV3`` with a world union now: rejected, since any
  registered-model change forces a bundle regeneration, and regeneration is
  blocked by pre-existing set-1 drift on the base commit (the
  ``experiment_proposal`` description). Unifying the pack and reservation
  readers into one ``CapsuleV3`` model is the follow-up once M00 resolves
  that drift; the shared layout, provenance and checksum make it mechanical.

### Generalizing to the next non-pack world

A second world repeats the reservation shape: a reviewed manifest module
mirroring its adapter (cross-checked in tests), scenario/run mirrors
reusing the world leaf types, a v3-shaped payload reusing ``CapsuleV3Version``,
``CapsuleProvenance`` and the checksum gates, detection-based
``faults_fired``, and a pinned ``ExternalDependency`` for any vendored code.
Nothing in the existing versions changes.
