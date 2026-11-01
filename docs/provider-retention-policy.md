# Provider retention policy for the current demo

Decision date: 1 November 2026. Scope: synthetic development/demo content only.

The application must not send customer records, personal documents, credentials or
unclassified material through the live provider path. The configured operator records
an exact provider, endpoint, model, review date and account-control verification before
the new retention guard allows that path. The decision expires after 30 days. Existing
live authorization and compatibility checks are still required; this policy grants no
permission to make paid calls.

For Baseten Model APIs, the published baseline is its
[security practices](https://www.baseten.co/security-practices/), which describe zero
content retention with product/configuration exceptions. Confirm that the account uses
that product and no optional payload logging or third-party retention before recording
an account verification. That confirmation has not been performed for this account.

For direct Meta Responses, stored continuation and account retention controls have to
be reviewed together. The current adapter is not changed here. Do not claim ZDR from a
request flag alone. Live use remains blocked until the exact account/control decision
has been recorded. The official references are the
[Responses schema](https://dev.meta.ai/docs/api-reference/responses/schemas) and
[ZDR guidance](https://dev.meta.ai/help/policies-and-privacy/zero-data-retention).

This policy does not rewrite the existing local retention windows or delete audit
evidence. Local stores remain private; any export, pruning or deletion follows the
project's existing M04/M09 procedures. A customer-data policy and deletion commitment
require a separate review before customer-connected use.
