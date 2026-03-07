# odoo-docker

Base Odoo runtime image.

This repository owns the base runtime build for Odoo 19. It compiles a
deterministic runtime from the upstream Odoo source, then layers in `uv`,
PostgreSQL 17 client tools, and compatibility paths used by downstream
deployment tooling.

This repository provides a stable base runtime for downstream project images.

## Images

- `runtime`: base Odoo runtime + PostgreSQL client + uv tooling
- `runtime-devtools`: `runtime` plus Chromium test tooling

Both images default to the `ubuntu` user for compatibility with existing
restore and SSH mount workflows.

## Devtools Addon Paths

- `runtime` stays runtime-first and does not write IDE-oriented Python path
  entries.
- `runtime-devtools` writes a minimal `odoo_paths.pth` for generic non-core
  source and addon roots used in local development tooling:
  - `/odoo`
  - `/opt/project/addons`
  - `/opt/extra_addons`

## CLI Contract

- `/odoo/odoo-bin` is a compatibility wrapper over upstream
  `/usr/local/bin/odoo-source-bin`.
- The wrapper must preserve Odoo subcommands (`server`, `shell`, `db`, etc.).
- Runtime defaults (`--db_host`, `--addons-path`, etc.) are injected only for
  server-style invocations so non-server commands keep upstream argument
  parsing semantics.

## CI Release Model

- Every run builds test images first and executes smoke checks.
- Publish only happens after smoke checks pass.
- `schedule` (daily) publishes `nightly-*` tags and immutable `sha-*` tags.
- `push` to `main` publishes stable `19.0-*` tags and immutable `sha-*` tags.
- `pull_request` runs verify-only (no image publishing).

This lets us keep a daily canary stream while protecting stable tags behind the
same verification gate.

## CI Cache Policy

- Builds use `cache-from` / `cache-to` with `type=gha` as the primary cache.
- Buildx builders are ephemeral in CI; the workflow does not persist local
  BuildKit state between runs.

This avoids unbounded local cache growth on self-hosted runners while still
keeping cross-run cache reuse through GitHub Actions cache storage.

The GHCR retention workflow keeps stable and nightly tags, preserves the newest
10 immutable `sha-*` tags per image suffix, and prunes untagged versions older
than 7 days.

## Runner Health Checks

- A scheduled `Runner Health` workflow tracks root filesystem and Docker root
  usage on `chris-testing` every six hours.
- The check fails when usage crosses the configured thresholds so operators get
  a visible GitHub Actions alert before the runner reaches saturation.

## Source Pinning

The workflow resolves the current `odoo/odoo` `19.0` commit and pins that exact
revision into the build. This gives repeatable artifacts per run and makes
nightly updates explicit.

`uv` is copied from Astral's official container image and pinned by tag+digest
in the Dockerfile. A GitHub-native Dependabot config watches that image
reference and opens update PRs whenever a new `uv` release is available.

## Build

```bash
docker build \
  -t ghcr.io/cbusillo/odoo-docker:19.0-runtime \
  --target runtime \
  .
docker build \
  -t ghcr.io/cbusillo/odoo-docker:19.0-devtools \
  --target runtime-devtools \
  .
```

## Security Notes

- Do not add credentials or access tokens in this repo.
- Proprietary addons should be fetched by downstream builds using BuildKit
  secrets.
