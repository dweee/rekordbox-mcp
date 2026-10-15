# rekordbox-mcp fork

Upstream: https://github.com/davehenke/rekordbox-mcp

Base revision: `d8dce67700911b6bef84b25c5f93ae07f200a1c5`. Upstream ancestry and license attribution are retained.

Standalone Nix packaging and track relinking with explicit previews, audio inspection, backups and rollback.

## Build

```sh
nix build .#packages.<system>.<package>
```

Supported packages and native systems are listed in `nix/platforms.json`.

## CI

Build and upstream-sync drafts are under `ci/disabled/` and are inactive there.
Darwin and Linux builds are separate where supported. See
[ci/ATTIC.md](ci/ATTIC.md) for optional binary-cache publication and
[ci/UPSTREAM-SYNC.md](ci/UPSTREAM-SYNC.md) for upstream maintenance.
