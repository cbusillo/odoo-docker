# odoo-docker

Base Odoo runtime image.

This repository owns the base runtime build for Odoo 19. It compiles a
deterministic runtime from the upstream Odoo source, then layers in `uv`,
PostgreSQL 17 client tools, and compatibility paths used by downstream
deployment tooling. The runtime also includes the Launchplane substrate needed
by Launchplane-managed Odoo lanes.

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

## Launchplane Runtime Substrate

The image reserves `/opt/launchplane/addons` for image-owned Odoo addons that
support Launchplane-managed runtime behavior. These addons are separate from
upstream Odoo source, downstream project addons in `/opt/project/addons`, and
shared business addons in `/opt/extra_addons`.

`launchplane_runtime_health` is loaded as a server-wide module by default. It
exposes `GET /launchplane/health` with `auth="none"` and `save_session=False`
so Launchplane can verify the exact runtime serving a public lane without
depending on tenant database or Website state. The endpoint returns JSON shaped
like:

```json
{"runtime_identity":null,"status":"pass"}
```

When `LAUNCHPLANE_RUNTIME_IDENTITY_JSON` is present and valid, the parsed object
is returned as `runtime_identity`. If the variable is present but malformed, the
endpoint returns a failing response instead of echoing raw environment text.

Downstream runtime overrides must preserve `/opt/launchplane/addons` in the
effective addons path and `base,web,launchplane_runtime_health` in the effective
server-wide modules. `/odoo/odoo-bin` normalizes server-mode invocations to keep
those image-owned defaults present.

## CLI Contract

- `/odoo/odoo-bin` is a compatibility wrapper over upstream
  `/usr/local/bin/odoo-source-bin`.
- The wrapper must preserve Odoo subcommands (`server`, `shell`, `db`, etc.).
- Runtime defaults (`--db_host`, `--addons-path`, etc.) are injected only for
  server-style invocations so non-server commands keep upstream argument
  parsing semantics.
- Server-style invocations always include `/opt/launchplane/addons` in the
  effective addons path and `launchplane_runtime_health` in the server-wide
  module list while preserving `base,web`.

## Downstream Build Contract

- Downstream images inherit a single shared Python environment at `/venv`.
- `/venv` is not configurable; downstream images must extend it additively and
  must not recreate it.
- The image reserves these downstream layout paths:
  - `/opt/runtime`
  - `/opt/project`
  - `/opt/project/addons`
  - `/opt/extra_addons`
- The image reserves `/opt/launchplane/addons` for image-owned Launchplane
  runtime addons. Downstream images must not replace this directory.
- Layout 2 is selected by `/opt/runtime/.odoo-python-sync-layout` containing
  `2`. Each populated dependency root must contain both `pyproject.toml` and
  `uv.lock`, plus `.odoo-python-source.json` with an `owner/repository`
  identity, exact lowercase 40-character source commit, and optional
  repository-relative `lock_path`. Legacy two-field markers default the lock
  path to `uv.lock`.
- `/opt/runtime` owns the support/runtime lock. `/opt/project` owns the tenant
  uv workspace lock. Both root pyprojects are static dependency catalogs with
  `tool.uv.package = false` and no build system. The helper checks and exports
  the support lock first and the tenant lock second with frozen semantics, then
  installs both exports in one dependency-free operation that cannot replace
  packages inherited from the base image.
- Layout 2 tenant workspace members must exactly match owned addon projects
  under `/opt/project/addons`. Owned projects use static package metadata,
  `tool.uv.package = false`, exact build-tool versions supplied by a lock, and
  code and package-metadata installation with `--no-deps` and no build
  isolation. A nested `.odoo-python-source.json` applies to projects beneath
  that directory, so staged shared-addon projects retain their own repository
  and commit attribution instead of inheriting the tenant root marker. Owned
  `requirements*.txt`, mutable VCS references, and local or archive dependency
  paths fail closed.
- Support-only and tenant-only layout 2 roots are accepted for staging and
  tests but emit non-publishable evidence. Support-only layouts may still carry
  ordinary addon code when it has no Python dependency metadata. Publishable
  artifacts require both lock scopes.
- `ODOO_PYTHON_SYNC_SKIP_ADDONS` remains a legacy-layout compatibility option.
  Layout 2 rejects it because skipping installation while exporting the full
  workspace lock would produce misleading dependency evidence.
- Without the layout marker, `odoo-python-sync.sh <prod|dev>` preserves the
  bounded legacy single-root behavior and writes non-publishable evidence. The
  legacy path remains available only while downstream producers migrate.
- `odoo-fetch-addons.sh` fetches external addon repositories declared in
  `ODOO_ADDON_REPOSITORIES` into `/opt/extra_addons`. Every entry must use
  `owner/repository@<exact-commit>`; branch and tag refs fail closed. The helper
  verifies the fetched Git commit before removing repository metadata and stores
  authoritative source sidecars beside checkouts rather than trusting marker
  files committed inside external repositories.
- Layout 2 treats external dependency files as an explicit compatibility lane.
  Their repository commit, repo-relative path, format, and SHA-256 are recorded;
  they may add compatible packages but cannot replace base- or lock-owned
  package identities.
- Every layout 2 sync finishes with `uv pip check` and writes deterministic
  platform-specific package evidence to
  `/opt/launchplane/evidence/dependency-provenance.json`. The producer combines
  one sidecar per target platform before publishing artifact provenance.
- Neither helper bakes in project-specific policy; downstream images choose
  which external repositories to fetch and whether to sync `prod` or `dev`
  dependencies.

## CI Release Model

- Every run builds test images first and executes smoke checks.
- Publish only happens after smoke checks pass.
- `schedule` (weekly) publishes `nightly-*` tags and immutable `sha-*` tags.
- `push` to `main` publishes stable `19.0-*` tags and immutable `sha-*` tags.
- `pull_request` runs verify-only (no image publishing) and reports one stable
  `image-verification` merge gate after lint, source resolution, override
  validation, both image builds, smoke tests, and vulnerability scans pass.
- Fork pull requests fail closed before using trusted self-hosted runners. A
  maintainer must recreate an accepted fork change on a branch in this
  repository before it can pass the image-verification merge gate.

This lets us keep a weekly canary stream while protecting stable tags behind the
same verification gate.

## CI Cache Policy

- Verify jobs run on the `chris-testing-build` self-hosted lane and reuse a
  persistent per-runner Buildx builder for single-platform smoke images. Pull
  request verification uses a per-run apt refresh epoch so Ubuntu package
  layers are rehydrated for each verification run.
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

- `bash scripts/test-check-requirements-overrides.sh` checks exact pins,
  explicitly allowed unpinned requirements, non-exact constraints, and removed
  requirements for the override freshness gate.
- `scripts/test-odoo-bin-wrapper.sh` checks wrapper argument handling without a
  container build.
- `scripts/smoke-runtime.sh <image-reference>` checks the runtime image helper
  contract and Odoo CLI availability.
- `scripts/smoke-devtools.sh <image-reference>` checks the devtools image
  browser tooling and addon path setup.
- `scripts/smoke-db-init.sh <image-reference>` checks database-backed Odoo
  initialization.
- `scripts/test-downstream-helpers.sh <image-reference>` checks downstream
  Python sync and external addon fetch behavior, including legacy compatibility,
  strict partial and layered locks, stale and incomplete lock failures, owned
  workspace enforcement, exact external source handling, conflict isolation,
  final environment checks, and deterministic evidence.

## Security Notes

- Do not add credentials or access tokens in this repo.
- Proprietary addons should be fetched by downstream builds using BuildKit
  secrets via `odoo-fetch-addons.sh`.
- `requirements-overrides.txt` carries minimum secure Python compatibility
  pins when upstream Odoo requirements cannot yet resolve a fixed transitive
  release. Keep those pins narrow and let
  `scripts/check-requirements-overrides.py` reject removed upstream
  requirements, unexpected unpinned or ranged requirements, and upstream exact
  pins that make an override removable.
