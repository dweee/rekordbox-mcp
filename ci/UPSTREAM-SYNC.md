# Upstream maintenance

The installed caller uses [dweee/dave-fork-ci](https://github.com/dweee/dave-fork-ci)
at `b5ad6e59b40ac1602172981bd28109c61798709f`. `.fork-ci.json` declares the upstream,
required custom files, native builds, regression commands and any history filter.

`FORK_CI_ENABLED=true` enables candidate updates. Automatic promotion remains off
until build/cache acceptance, then requires both `auto-merge: true` in the caller
and `FORK_CI_AUTO_MERGE=true`. Attic uploads use the separately scoped repository
secret described in [ATTIC.md](ATTIC.md). Every successful build retains a closure
archive independently of Attic. Filtered forks must use the same `history_exclude`
policy for their imported base and every upstream update.
