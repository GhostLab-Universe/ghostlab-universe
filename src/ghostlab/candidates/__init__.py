"""Controlled-improvement candidate lifecycle (milestone track T5, Release 2 core).

Developer-supplied mode first: a candidate selects a changed *registered*
agent/service version, optionally with allowlisted tunable-field overlays.
Every candidate carries its parent version, the exact diff, config/code
identity, evidence references, and a status with a rejection reason.
Evaluations are multidimensional (original failing scenario, development
regressions, completion, fault coverage, usage) with no aggregate hiding
the dimensions. Promotion binds to the existing single-use digest-bound
human approval flow (``policy/approvals.py``), reused by import.

Bounded generated-candidate mode: the contract and validation for
model-proposed config-field changes are defined here, but no live model
is wired and generated-code execution stays refused structurally (there
is no code execution path; ``kind: code`` diffs are refused).

Standard library only in ``records``, ``allowlist``, ``store``,
``demo_targets`` and ``evaluate``. ``service`` binds the approval flow
and therefore needs the contracts extra. Zero model calls anywhere.
"""
