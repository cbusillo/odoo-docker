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
- VCS package evidence uses `owner/repository` identities for GitHub sources
  and sanitized HTTPS or SSH URLs for other hosts, with any terminal `.git`
  suffix removed. `packages_sha256` hashes the exact canonical package array
  embedded in the evidence.
- Neither helper bakes in project-specific policy; downstream images choose
  which external repositories to fetch and whether to sync `prod` or `dev`
  dependencies.

## CI Release Model

- Every run builds test images first and executes smoke checks.
- Pull-request builds resolve one Ubuntu snapshot, exact PostgreSQL package set,
  Python/npm cutoff, scanner image, and Trivy database for the comparison.
  Exact base and head commits use those same inputs. The merge gate blocks
  introduced or worsened high/critical findings without blaming a pull request
  for unchanged inherited findings. Causal comparison runs only when the base
  has native resolver support and its workflow, scanner, and evaluator match
  the head. The comparison uses the base copies of that tooling. Otherwise,
  verification retains the stricter candidate absolute gate, using base scanner
  tooling when available.
- Default-branch, scheduled, and dispatch runs retain absolute high/critical
  health. Multi-architecture images are pushed first under a unique
  `candidate-<run>-<repository-sha>-*` tag, then the exact registry manifest and
  its `linux/amd64` and `linux/arm64` child digests are scanned. Stable, nightly,
  source `sha-*`, and validated `build-*` aliases move only after that evidence
  passes and each alias is verified against the scanned manifest digest.
- Trivy skips only `/usr/share/java/gettext.jar` and
  `/usr/share/java/libintl-0.21.jar`, which are owned by Ubuntu's gettext
  packages, so comparison scans do not require a separate mutable Java
  database; the installed gettext packages remain covered by the Ubuntu OS
  advisory scan.
- `schedule` (weekly) promotes `nightly-*`, `sha-*`, and `build-*` tags.
- `push` to `main` promotes stable `19.0-*`, `sha-*`, and `build-*` tags.
- `pull_request` runs verify-only (no image publishing) and reports one stable
  `image-verification` merge gate after lint, source resolution, override
  validation, both image builds when resolver parity is available, smoke tests,
  and dependency-health evaluation pass.
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

The GHCR retention workflow keeps stable, nightly, and registry-cache tags,
preserves the newest 10 `sha-*`, validated `build-*`, and `candidate-*` tags per
image suffix, and prunes untagged versions older than 7 days.

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

The Dockerfile pins artifact-affecting base and build images by digest, uses an
exact uv-managed Python patch release, and accepts workflow-resolved Ubuntu
snapshot, PostgreSQL package, and package-index cutoff inputs. Trivy snapshots
record repository/source identity, producer and database revisions, scan scope,
configuration hash, and platform. Publication evidence additionally records the
exact manifest-list digest, platform child digests, and snapshot hashes.

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
- `bash scripts/test-image-dependency-health.sh` checks deterministic Trivy
  normalization, fail-closed provenance matching, regression policy, target
  advisory handling, and multi-architecture artifact binding.
- `scripts/test-odoo-bin-wrapper.sh` checks wrapper argument handling without a
  container build.
- `scripts/smoke-runtime.sh <image-reference>` checks the runtime image helper
  contract and Odoo CLI availability.
- `scripts/smoke-devtools.sh <image-reference>` checks the runtime contract
  before checking the devtools image browser tooling and addon path setup.
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
- `requirements-overrides.txt` carries reviewed secure or shared downstream
  compatibility pins when upstream Odoo requirements cannot yet resolve the
  required release. Keep those pins narrow and let
  `scripts/check-requirements-overrides.py` reject removed upstream
  requirements, unexpected unpinned or ranged requirements, and upstream exact
  pins that make an override removable.
- Neither the runtime virtual environment nor its uv-managed base interpreter
  under `/opt/uv/python` ships `pip` or `ensurepip`. Image builds and downstream
  dependency synchronization use `uv`; restoring `pip` would add unused
  vendored packages and expand the runtime vulnerability and supply-chain
  surface. Standard-library virtual environment bootstrapping with bundled
  `pip` is intentionally unavailable; use `uv venv` instead.
