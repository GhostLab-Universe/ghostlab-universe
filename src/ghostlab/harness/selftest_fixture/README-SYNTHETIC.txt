SYNTHETIC SELF-TEST FIXTURE -- NOT HIDDEN EVALUATION CONTENT
============================================================

Everything in this directory is the harness author's own dev defect set for
exercising the verdict pipeline end to end. It is NOT the hidden evaluation
set, it has never seen that set, and it must never be mistaken for held-out
evidence:

- manifest.json declares namespace "synthetic-selftest" (run_selftest only;
  run_heldout refuses it).
- Every case id carries the SYNTH- prefix, which held-out manifests forbid.
- Every spec embeds its candidate_script, which held-out specs forbid.
- selftest_config.json is the frozen-config handshake input for self-tests.

Expected verdicts: SYNTH-PASS-1 -> pass, SYNTH-VIOLATION-1 -> violation
(naive fresh-key retry after a timeout: DEV-R1 shape), SYNTH-UNKNOWN-1 ->
unknown (run-record actor withheld, so the actor rule cannot decide).
