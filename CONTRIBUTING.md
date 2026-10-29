# Working together

Use one branch per task, such as feature/refund-world or feature/reality-diff. Open a pull request with the behavior changed, how it was checked and any known gaps. Request review from the other collaborator before merging.

Keep commits small enough to review. Agree on changes to experiment schemas before either person updates dependent code. Record major decisions in architecture.md.

Never commit API keys, local environment files, production records or billing screenshots. Commit an environment example with empty values when integrations are added. Use synthetic fixtures for shared experiments.

Meaningful checks should cover state transitions, replay determinism, invariant failures and repair regressions. Documentation-only changes need a link and accuracy check.

The initial scaffold has no license grant. Choose a license together before inviting outside contributions or publishing the code.

