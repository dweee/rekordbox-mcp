# Upstream maintenance

`.fork-ci.json` declares the upstream, native build matrix and regression commands.
The inactive caller is `ci/disabled/upstream-sync.yml`.

The caller uses the reusable `dave-fork-ci` workflow. Before installation, publish
that workflow under `.github/workflows/upstream-sync.yml` in the controller
repository, then regenerate this caller with its real owner/repository and new
40-character release commit. Use that same commit for the workflow reference and
`tool-revision`. The placeholder owner and draft source pin are not a live release.

The workflow polls the configured branch and latest stable release, merges into
a candidate branch, and runs all required builds, checks and tests. Optional Attic
uploads precede promotion. Conflicts, protected-file changes, moved tags, failed
checks or a changed default branch stop promotion.

Enable `FORK_CI_ENABLED` for a supervised run. Automatic promotion additionally
requires `FORK_CI_AUTO_MERGE`; cache publication requires the variables and scoped
secret documented in [ATTIC.md](ATTIC.md). Hosted builds and authenticated uploads
must be verified before enabling automatic promotion.

If `history_exclude` is configured, the initial imported history and every
upstream sync must use that same filter. Changing exclusions requires a local
history review; the controller will stop rather than change the mapping silently.
