# odoo-docker

Base Odoo runtime image.

This repository owns the base runtime build for Odoo 19. It compiles a
deterministic runtime from the upstream Odoo source, then layers in `uv`,
PostgreSQL 17 client tools, and compatibility paths used by downstream
deployment tooling.

This repository provides a stable base runtime for downstream project images.

## Images

- `runtime`: base Odoo runtime + PostgreSQL client + uv tooling
- `runtime-devtools`: `runtime` plus Playwright-managed Chromium test tooling

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
- `runtime-devtools` exposes Playwright-managed Chromium through `CHROME_BIN`
  at `/usr/local/bin/chromium-playwright`.

## CLI Contract

- `/odoo/odoo-bin` is a compatibility wrapper over upstream
  `/usr/local/bin/odoo-source-bin`.
- The wrapper must preserve Odoo subcommands (`server`, `shell`, `db`, etc.).
- Runtime defaults (`--db_host`, `--addons-path`, etc.) are injected only for
  server-style invocations so non-server commands keep upstream argument
  parsing semantics.

## Downstream Build Contract

- Downstream images inherit a single shared Python environment at `/venv`.
- `/venv` is not configurable; downstream images must extend it additively and
  must not recreate it.
- The image reserves these downstream layout paths:
  - `/opt/project`
  - `/opt/project/addons`
  - `/opt/extra_addons`
- `odoo-python-sync.sh <prod|dev>` installs root lockfile-backed dependencies
  plus addon `requirements*.txt` and addon `pyproject.toml` dependencies into
  `/venv`.
- `ODOO_PYTHON_SYNC_SKIP_ADDONS` can exclude a comma-separated set of addon
  directory names from Python dependency sync when a downstream workflow needs
  addon code on the path without packaging it into `/venv`.
- `odoo-fetch-addons.sh` downloads external addon repositories declared in
  `ODOO_ADDON_REPOSITORIES` into `/opt/extra_addons`.
- Neither helper bakes in project-specific policy; downstream images choose
  which external repositories to fetch and whether to sync `prod` or `dev`
  dependencies.

## CI Release Model

- Every run builds test images first and executes smoke checks.
- Publish only happens after smoke checks pass.
- `schedule` (weekly) publishes `nightly-*` tags and immutable `sha-*` tags.
- `push` to `main` publishes stable `19.0-*` tags and immutable `sha-*` tags.
- `pull_request` runs verify-only (no image publishing).

This lets us keep a weekly canary stream while protecting stable tags behind the
same verification gate.

## CI Cache Policy

- Verify jobs run on the `chris-testing-build` self-hosted lane and still use
  ephemeral Buildx builders with `type=gha` cache because they only build
  single-platform smoke images.
- Publish jobs run on the `chris-testing-publish-cache` self-hosted lane and
  reuse a persistent per-runner Buildx builder, with GHCR registry cache as
  the portable fallback.
- The publish workflow prunes cache entries older than 14 days after each run
  so local BuildKit state stays warm without growing forever.

This keeps the expensive multi-arch publish path warm on the self-hosted runner
while still giving us a recoverable remote cache when a builder is recreated.

The GHCR retention workflow keeps stable and nightly tags, preserves the newest
10 immutable `sha-*` tags per image suffix, and prunes untagged versions older
than 7 days.

GHCR retention is repo-local package hygiene. This repo owns the package tags it
publishes, so the retention workflow may delete only this repo's old package
versions; it does not own Launchplane runtime state, tenant deployments,
provider targets, product profiles, managed secrets, or artifact evidence.

## Runner Health Checks

- A scheduled `Runner Health` workflow tracks root filesystem and Docker root
  usage on `chris-testing` daily.
- The health workflow runs on `chris-testing-publish-cache` so it does not
  consume the `chris-testing-build` verification lane.
- The check fails when usage crosses the configured thresholds so operators get
  a visible GitHub Actions alert before the runner reaches saturation.

Runner health is repo-local image-build lane telemetry. It tracks the
self-hosted cache lane used by this image build workflow and does not replace
Launchplane-owned runner-host hygiene, runtime inventory, or provider health
records.

## Source Pinning

The workflow resolves the current `odoo/odoo` `19.0` commit and pins that exact
revision into the build. This gives repeatable artifacts per run and makes
scheduled updates explicit.

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

## Validation Commands

- `scripts/test-odoo-bin-wrapper.sh` checks wrapper argument handling without a
  container build.
- `scripts/smoke-runtime.sh <image-reference>` checks the runtime image helper
  contract and Odoo CLI availability.
- `scripts/smoke-devtools.sh <image-reference>` checks the devtools image
  browser tooling and addon path setup.
- `scripts/smoke-db-init.sh <image-reference>` checks database-backed Odoo
  initialization.
- `scripts/test-downstream-helpers.sh <image-reference>` checks downstream
  Python sync and external addon fetch behavior.

## Security Notes

- Do not add credentials or access tokens in this repo.
- Proprietary addons should be fetched by downstream builds using BuildKit
  secrets via `odoo-fetch-addons.sh`.
