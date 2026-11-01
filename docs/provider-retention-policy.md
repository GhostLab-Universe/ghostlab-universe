# Provider retention policy for this draft API

Scope: local synthetic development/demo only. No real IdP or model account has been qualified.

**The API refuses live inference for all session adapters**, even with an operator label,
retention decision or M05 live authorization. An authorized live configuration returns 403 live_content_not_qualified; an unauthorized
one retains the existing 403 inference_not_authorized response.
Fixture and scripted runs remain available. Read/replay/cancel remain available. This is a
scope reduction from the first draft, whose synthetic label did not inspect actual content.
A name, phone number or identity number cannot be made safe by labelling it synthetic.

RetentionDecision only validates review metadata: provider, endpoint, model, reviewer,
policy URL, aware review date, 30-day expiry and verified account controls. Meta additionally
requires an explicit continuation_policy (stateful-provider-storage-reviewed or
stateless-provider-storage-reviewed). This field records the operator's reviewed choice;
it neither configures the Meta adapter nor certifies provider deletion. Even a valid record
does not enable RetentionCheckedFactory.complete().

Future live API use requires an independently reviewed outbound-content/provenance boundary
and exact-model compatibility/paid-call authorization, plus verified provider-account controls.
The standalone M01 probe is outside this API policy and retains its separate authorization gate.

Published references for that future review:
- [Baseten security practices](https://www.baseten.co/security-practices/).
- [Meta Responses schema](https://dev.meta.ai/docs/api-reference/responses/schemas).
- [Meta retention guidance](https://dev.meta.ai/help/policies-and-privacy/zero-data-retention).

No provider setting is changed here. No local deletion/pruning scheduler is implemented or
claimed to exist in M09. Local retention, backups, deletion and audit rules remain explicit
unfinished customer-use requirements; customer-connected use stays disabled.
