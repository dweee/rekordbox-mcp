# Inactive package builders and Attic publication

These workflows remain under `ci/disabled/`, outside recognized workflow
locations. Forgejo Actions is disabled. Preparing this integration does not run
builds, upload packages, create caches, change credentials or activate machines.

## Installer and platform support

All drafts use `nixbuild/nix-quick-install-action` v35, pinned to commit
`9f63be77f412a248c9d9a65a4c82cf066cdf8f0c`, and explicitly select Nix 2.34.7.
This action installs single-user Nix on GitHub-hosted runners. These drafts
are intended for GitHub-hosted Apple Silicon macOS and x86/ARM Linux after
publication, rather than existing self-hosted Forgejo workers.

Source: https://github.com/nixbuild/nix-quick-install-action/tree/9f63be77f412a248c9d9a65a4c82cf066cdf8f0c

## Configure at go-live

Configure the following in each repository that will run package jobs:

* The workflow's existing CI enable variable: `DAVE_PACKAGES_CI_ENABLED=true`
  in the catalog, or `DAVE_FORK_CI_ENABLED=true` in application forks.
* Variable `ATTIC_PUSH_ENABLED=true` to publish successful builds.
* Variable `ATTIC_ENDPOINT`: the HTTPS URL of your existing Attic server.
* Variable `ATTIC_CACHE`: the existing cache name, without a server prefix.
* Secret `ATTIC_TOKEN`: a dedicated, expiring token scoped to pull/push that
  cache. It does not need cache creation, configuration, deletion or admin rights.

An administrator can issue that scoped token on the Attic server with
`atticadm make-token --sub github-packages --validity 90d --pull CACHE --push CACHE`.
Keep the result private and enter it using the hosting service's secret UI.
No existing host credentials are copied into the repositories. The upload token
will be available to GitHub's runner when the publication step goes live.

Optional variables `ATTIC_SUBSTITUTER` (the cache's public Nix substituter URL)
and `ATTIC_PUBLIC_KEY` (its public signing key) let builds reuse existing cache
entries. Configure both for an anonymously readable cache. Private cache reads
are not configured here; no write token is exposed to the build step. Without
these variables builds read from cache.nixos.org and still upload to Attic.

Move only the approved workflow drafts into `.github/workflows/` when ready.
Dispatch a manual run on the repository's default branch, selecting one package
for the first timing and end-to-end cache check. Application forks own their
own jobs; the catalog excludes their aliases. The catalog's private Forgejo
inputs must be replaced with approved public sources before a public GitHub run.

## Publication behavior

Only successful manual runs on the default branch can upload. Builds on another
branch remain build-only. A failed build never reaches publication. Enabling
uploads with missing/invalid settings or a failed Attic upload fails the job;
there is no success claim or silent fallback for a package that was not cached.

The workflow installs `attic-client` from that repository's locked nixpkgs input,
then passes only `nix build --print-out-paths` output roots to `ci/push-attic.py`.
`attic push` includes their runtime closures. It does not upload the entire runner
store, install deployment hooks or change fleet cache promotion rules.

A temporary mode-0600 Attic configuration under the runner temporary directory
holds the step's token. The helper removes the token from the child environment,
never passes it as a command argument, and removes the temporary configuration
after both successful and failed uploads. No token enters Nix configuration,
Nix derivations, Git files or artifacts.

Build summaries record package build duration and successful upload duration.
The publication step has a 30-minute timeout within the existing job timeout.
There are no automatic retries of authentication or other upload failures.

A successful upload does not prove a later machine can substitute the package:
verify the same output store path is readable from the selected cache with the
intended consumer credentials and signing-key trust. Consumers also need matching
inputs and recipes. No fleet configuration is changed by these workflows.

Reference CLI behavior: https://docs.attic.rs/reference/attic-cli.html
Token scopes: https://docs.attic.rs/reference/atticadm-cli.html
