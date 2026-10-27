# odoo-docker

Base Odoo runtime image.

This repository owns the Community base runtime builds declared in
[image-variants.json](image-variants.json). It builds a
runtime from resolved upstream Odoo source, then layers in `uv`,
PostgreSQL 17 client tools, and compatibility paths used by downstream
deployment tooling. The runtime also includes the Launchplane substrate needed
by Launchplane-managed Odoo lanes.

This repository provides a stable base runtime for downstream project images.

## Direction and Contributions

The Director's [overall DIRECTION.md](https://github.com/cbusillo/direction/blob/HEAD/DIRECTION.md)
sets priorities and stop boundaries. This repository has no separate
DIRECTION.md. [AGENTS.md](AGENTS.md) is the agent-instruction entry point;
[`.github/github.json`](.github/github.json) records validation commands and
repository workflow metadata.

Use the maintained [executing loop](https://github.com/cbusillo/codex-skills/blob/main/skills/references/executing-loop.md)
for issue-backed work. Image publishing follows the CI release model below;
tenant runtime deployment is owned by Launchplane.

## Images

- `runtime`: base Odoo runtime + PostgreSQL client + uv tooling
- `runtime-devtools`: `runtime` plus Playwright-managed Chromium test tooling

Both images default to the `ubuntu` user for compatibility with existing
restore and SSH mount workflows.

## Local Python Inspections

`uv sync --locked` installs the development-only dependencies for the Python
helpers and native PyCharm inspections. This environment does not determine
image dependencies. The Docker build excludes `.venv`, `pyproject.toml`, and
`uv.lock` from its context.

The inspection helper prepares an ignored project model and interpreter for
each worktree using `.github/github.json`. Shared inspection profiles, scopes,
and repository-local VCS mappings remain versioned; sibling checkouts and SDK
paths do not.

The runtime health addon also needs real Odoo 19 source for import resolution.
Choose a clean external checkout at an exact reviewed commit, then expose that
source to this worktree's interpreter before running the inspection helper:

```bash
ODOO_IDE_SOURCE=/absolute/path/to/odoo \
ODOO_IDE_COMMIT='<full-40-character-commit>' \
uv run --locked python - <<'PY'
import os
from pathlib import Path
import subprocess
import sysconfig

source = Path(os.environ["ODOO_IDE_SOURCE"]).resolve()
expected = os.environ["ODOO_IDE_COMMIT"]
assert len(expected) == 40 and all(c in "0123456789abcdef" for c in expected)
assert not source.is_relative_to(Path.cwd().resolve())
assert (source / "odoo/http.py").is_file()
assert subprocess.check_output(
    ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
).strip() == expected
assert not subprocess.check_output(
    ["git", "-C", str(source), "status", "--porcelain"], text=True
)
Path(sysconfig.get_path("purelib"), "odoo-ide-source.pth").write_text(
    str(source) + "\n", encoding="utf-8"
)
PY
```

Keep the source checkout outside the inspected project. This supplies the
actual framework API without copying upstream source or adding import stubs to
the image repository.

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

The image-owned health addon uses Odoo's native short version format, which
the selected runtime prefixes with its major version. Its manifest can be
installed on both 19 and 20. This does not qualify every image or downstream
addon for 20; the versioned image and tenant ports retain their own gates.

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

### Private database readiness

`POST /launchplane/readiness` is distinct from liveness. It accepts only direct
loopback probes inside the candidate; forwarded requests and peers in separate
network namespaces receive 403. A proxy sharing the candidate's network namespace
must exclude this route from public ingress or preserve forwarded headers; a
loopback peer alone cannot identify an unmarked local relay. Run the image-owned
`launchplane-readiness` helper through the
candidate's existing private execution channel. No new credential is needed.
Responses contain only `status` and a fixed failure reason, use `no-store`, and
never enumerate databases, configurations or credentials.

The versioned contract and validation are owned by
[`controllers/readiness.py`](launchplane/addons/launchplane_runtime_health/controllers/readiness.py).
[`scripts/fixtures/readiness/setup.py`](scripts/fixtures/readiness/setup.py)
provides a complete isolated example. Launchplane supplies the intended Host,
database and UUID, Website, expected installed/loaded modules, immutable runtime
identity (artifact, image digest, source commit, slot and release), exact update
receipt, and representative homepage/login/public paths with content assertions.
There are no tenant route or content defaults. The request sends both the real
Host and `X-Odoo-Database`; database filtering and Website selection must agree,
with no Website fallback or session cookie. Odoo 19's strict resolver and
Odoo 20's core resolver must bind the configured domain to the requested Host;
an unknown Host or a Website without a domain fails readiness. All pending install/update/removal
states and Odoo's [partially-updated marker](https://github.com/odoo/odoo/blob/e55c653b4dbafba45de5434c8da96db5952b42cb/odoo/modules/loading.py#L604)
fail readiness. Odoo writes that marker when module actions remain pending after
an update, and [registry startup](https://github.com/odoo/odoo/blob/e55c653b4dbafba45de5434c8da96db5952b42cb/odoo/orm/registry.py#L174)
consumes it to force another update.

The probe sends compact JSON without dropping asset evidence. The endpoint's
finite request bound accommodates complete contract and observation receipts,
including theme and gallery assets. Larger receipts fail closed and the CLI
identifies that size failure. Each tenant must qualify its own complete receipt. The
native smoke includes a published image gallery and checks every image hash,
plus rejection immediately beyond the runtime's request bound.
Known validation failures report their existing reason code without logging
the contract's private values.

The maintenance producer records `launchplane.readiness.release` in
`ir.config_parameter` **after** successful exact-release updates and the committed
credential boundary. Its receipt binds the full expected runtime identity and
resolved update list, and attests the mail/integration fences and committed
credential sanitizing. An empty update list is valid for a compatible release,
but still needs a fresh release receipt. This endpoint never strips keys, updates
modules, loads a registry deliberately, or changes a fence. Odoo startup/request
dispatch can load the registry before any controller runs: the candidate must
start only after the existing pre-registry credential boundary is committed.
Launchplane owns proving those fences and starting the candidate with crons
disabled (`--max-cron-threads=0`), isolated outbound networking and one writer
owner. A receipt is an attestation by that trusted coordinator, not independent
proof of host networking or arbitrary addon side effects.

Preparation and readiness are separate commands:

```bash
# Controlled preparation: can render and generate bundles/attachments.
launchplane-readiness prepare --contract /private/contract.json \
  --evidence /private/browser.json > /private/observations.json
# Observational readiness: no page fetches, warming or database writes.
launchplane-readiness check --contract /private/contract.json \
  --evidence /private/observations.json
```

Launchplane's controlled browser smoke provides the browser receipt, bound to
the contract hash and every configured path, after successful navigation,
expected content, script and response checks. The fixture's
[`browser.py`](scripts/fixtures/readiness/browser.py) demonstrates the receipt
with Chromium. Preparation first checks the candidate's database/release/fence
state through readiness; only its missing-observation response permits warming.
It then renders the configured pages, asserts content, and
fetches referenced CSS, JS and media, including CSS resources. Redirects,
missing/empty resources and unexpected asset content types fail preparation.
Cross-origin assets require explicit HTTPS `asset_origins` from runtime records
or scoped input. There are no origin defaults. Their requests carry no candidate
Host/database headers, cookies or credentials; redirects and ambient proxy
settings remain disabled. The coordinator must permit only the required public
asset destinations through its outbound fence. The isolated fixture uses local
system fonts and an internal Docker network; it never contacts a public CDN.
Readiness requires successful render/asset/browser evidence no older than 30
seconds, bound to the exact contract. Re-run preparation immediately before a
switch. These are observations at their recorded times, not a promise that a
later request will succeed. Launchplane coordinates the final switch and public
post-switch verification; HTTP 200 from liveness or `/web/health` is insufficient.

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
- A nonempty `ODOO_DB_PASSWORD` is forwarded as `PGPASSWORD` for every
  subcommand, overriding an inherited `PGPASSWORD`. Odoo reads it ahead of
  config-file passwords; the wrapper never
  generates a password argument. When it is empty or absent, an existing
  `PGPASSWORD` or config-file password keeps upstream behavior. Downstream
  callers should use these environment/config paths instead of password flags.

## Downstream Build Contract

- Downstream images inherit a single shared Python environment at `/venv`.
- `/venv` is not configurable; downstream images must extend it additively and
  must not recreate it.
- `/opt/launchplane/base-python-constraints.txt` is the base-owned Python
  package catalog: sorted canonical `name==version` lines for every distribution
  installed in `/venv` when the base is built. The image digest and target
  platform identify this catalog. Runtime, devtools and enterprise images
  inherit it unchanged; downstream images must not replace it. It is generated
  by the existing Python sync helper's `export-base-constraints` command before
  downstream installation. That command reports the current interpreter's
  environment; consumers read the baked file instead of regenerating it after
  extending `/venv`.
- Consumers read the catalog from the exact image digest and target platform
  used to build. The catalog does not authorize inherited-package replacement;
  the installer retains its independent checks against the live environment.
  Consumer adoption is tracked in
  [devkit #246](https://github.com/cbusillo/odoo-devkit/issues/246) and
  [OPW #258](https://github.com/cbusillo/odoo-tenant-opw/issues/258).
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
- In layout 2, external Odoo addons with only Whool's `build-system` metadata
  and an `__manifest__.py` are loaded directly from their verified source tree. Their
  repository must provide `requirements.txt`; layout 2 installs that file under
  the same compatibility constraints and records it alongside each addon's
  packaging metadata. Development sync also includes repository-level
  `requirements-dev.txt` when present. Whool is not installed or executed for
  this source-only addon path. Other external Python projects still require
  static package names and exact build-tool requirements.
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
  Each declared series resolves its own exact Odoo revision and verifies both
  runtime targets. Smoke tests check the actual installed Odoo series.
- Pull-request builds resolve one Ubuntu snapshot, exact PostgreSQL package set,
  Python/npm cutoff, scanner image, and Trivy database for the comparison.
  Exact base and head commits use those same inputs. The merge gate blocks
  introduced or worsened high/critical findings without blaming a pull request
  for unchanged inherited findings. Causal comparison runs only when the base
  declares that same series, has native resolver support, and its workflow, scanner, and evaluator match
  the head. The comparison uses the base copies of that tooling. Otherwise,
  verification retains the stricter candidate absolute gate, using base scanner
  tooling and its pinned scanner image when available.
- Default-branch, scheduled, and dispatch runs retain absolute high/critical
  health. Every same-repository PR retains both base-tooling comparison/fallback
  checks and candidate-tooling absolute health before merge, including source
  heads eligible for single-entry train reuse. They build each target
  once; matching tooling reuses the scan, while differing tooling scans that
  same image with its separately bound scanner profile and database. Both
  evaluations must pass. Multi-architecture images are pushed first under a unique
  `<alias-prefix>candidate-<run>-<repository-sha>-*` tag, then the exact registry manifest and
  its `linux/amd64` and `linux/arm64` child digests are scanned. Stable, nightly,
  source `sha-*`, and validated `build-*` aliases move only after that evidence
  passes and each alias is verified against the scanned manifest digest. Each exact
  platform child also executes its existing runtime/devtools smoke script. Both
  targets must pass before either target starts alias promotion. Interrupted
  registry updates can still leave a mixed pair; tag updates are not atomic.
- Trivy skips only `/usr/share/java/gettext.jar` and
  `/usr/share/java/libintl-0.21.jar`, which are owned by Ubuntu's gettext
  packages, so comparison scans do not require a separate mutable Java
  database; the installed gettext packages remain covered by the Ubuntu OS
  advisory scan.
- `schedule` (weekly) promotes `nightly-*`, `sha-*`, and `build-*` tags.
- `push` to `main` promotes each series' stable, source and build tags.
- Manual `workflow_dispatch` runs promote `sha-*` and `build-*` tags, and also
  each series' stable tags unless `publish_stable` is turned off (it defaults on).
  The legacy series keeps its existing unqualified nightly/source/build/cache
  names. Other series qualify candidate, nightly, source, build and cache names with their configured alias prefix;
  all stable tags use the series. Local verification tags, evidence and artifacts
  include the series to prevent cross-version collisions.
- Train candidates run full validation through their generated batch PR,
  reporting `image-verification` without publishing or a second train-push
  build. This repository uses merge commits without CODEOWNERS assignments;
  restore a candidate-push route before adopting a mode that omits the batch PR.
- Each publisher first requires both targets' successful verification provenance
  for its exact series, Odoo revision, repository commit, run and resolved inputs.
  A failure in one series cannot publish an unchecked target or prevent the
  other qualified pair from progressing. Published evidence uploads precede
  the digest records that permit alias promotion. Index and platform annotations
  bind the series and Odoo revision; promotion checks them and alias readback.
- Only the variant with Enterprise follow-up enabled dispatches `odoo-enterprise-docker`
  build with the exact published runtime and devtools digests. It passes on
  the same stable decision, and asks for nightly tags only on scheduled runs.
  Community 20 never triggers an Enterprise 19 build. Base-image aliases mean
  image qualification; tenant testing, Client acceptance and production release
  remain on their own gated paths.
- `pull_request` runs verify-only (no image publishing) and reports one stable
  `image-verification` merge gate after lint, source resolution, override
  validation inside each selected series, all supported image builds, smoke tests,
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
- After each run, the verify and publish jobs each prune builder cache older
  than 7 days while their builder holds more than 60 GB, so local BuildKit
  state stays warm without growing forever.

This keeps the expensive multi-arch publish path warm on the self-hosted runner
while still giving us a recoverable remote cache when a builder is recreated.

The GHCR retention workflow keeps stable, nightly, and registry-cache tags,
preserves the newest 10 `sha-*`, validated `build-*`, and `candidate-*` tags per
series and image suffix, and prunes unreferenced untagged versions older than 7 days.
Tag grammar comes from the same variant file; unreadable or invalid variant
configuration stops cleanup before any deletion.
Stable series tags and their referenced manifests remain protected even when
that series is removed from the active build list.
Platform manifests referenced by retained indexes are preserved regardless of
age. Cleanup stops before deleting anything when a retained index cannot be
inspected or a readable child cannot be reconciled with package inventory.
Children already missing from the registry are reported without preventing
protection of the remaining recoverable manifests.

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

The workflow resolves each declared `odoo/odoo` series' current commit and pins
that exact revision into its builds. This gives repeatable artifacts per run and makes
scheduled updates explicit.

The Dockerfile pins artifact-affecting base and build images by digest, uses an
exact uv-managed Python patch release, and accepts workflow-resolved Ubuntu
snapshot, PostgreSQL package, and package-index cutoff inputs. Trivy snapshots
record repository/source identity, producer and database revisions, scan scope,
configuration hash, and platform. Publication evidence additionally records the
exact manifest-list digest, platform child digests, and snapshot hashes.

Published image indexes and manifests also carry the image repository and exact
producer commit in OCI source/revision annotations. Unlike config labels, these
do not misattribute downstream images through inheritance. These annotations complement
digest-bound evidence; source and run tags remain aliases rather than immutable identity.

`uv` is copied from Astral's official container image and pinned by tag+digest
in the Dockerfile. Renovate checks that image reference and the workflow
actions weekly, waits 7 days after a release, and groups minor and patch updates
into one pull request.

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

For a pinned Community 20 build, set `ODOO_SOURCE_REV` to the full upstream
commit to qualify, then use:

```bash
docker build --target runtime \
  --build-arg ODOO_SOURCE_REF=20.0 \
  --build-arg ODOO_SOURCE_REV="${ODOO_SOURCE_REV:?Set an exact upstream commit}" \
  -t odoo-local:20.0-runtime .
```

Use `--target runtime-devtools` and a separate local tag for its browser layer.

## Validation Commands

- `bash scripts/test-published-promotion.sh` executes the real promotion shell
  with a disposable Docker adapter, proving an invalid pair causes no alias
  changes and valid records promote their exact checked manifests.

- `bash scripts/test-ghcr-retention.sh` executes registry cleanup against
  disposable API/manifest fixtures, including retained children and unavailable
  dependency evidence. It performs no registry writes.

- `bash scripts/test-check-requirements-overrides.sh` checks exact pins,
  explicitly allowed unpinned requirements, non-exact constraints, and removed
  requirements for the override freshness gate.
- `bash scripts/test-image-dependency-health.sh` checks the scanner's
  `--skip-files` arguments without a Docker daemon, deterministic Trivy
  normalization, fail-closed provenance matching, regression policy, target
  advisory handling, and multi-architecture artifact binding.
- `scripts/test-odoo-bin-wrapper.sh` checks wrapper argument handling without a
  container build.
- `scripts/smoke-runtime.sh <image-reference> [odoo-series]` checks the runtime
  image helper contract and Odoo CLI availability. Supply the selected series
  to also verify the installed Odoo identity, as CI does for each matrix row.
- `scripts/smoke-devtools.sh <image-reference> [odoo-series]` checks the runtime
  contract and selected series before checking the devtools image browser
  tooling and addon path setup.
- `scripts/smoke-db-init.sh <image-reference>` checks database-backed Odoo
  initialization and the installed state of `base` and `launchplane_runtime_health`,
  authenticated shell and database dump commands, and that
  disposable Odoo process listings omit database password values and flags.
  It also requires a successful, nonempty run of upstream Odoo's
  image/attachment tests against the installed Pillow. The selection retains
  `base:TestImage` where available and exercises
  `base:TestIrAttachment.test_10_image_autoresize` on both Odoo 19 and 20.
- `scripts/smoke-readiness.sh <runtime-image> [browser-image]` creates a fresh
  Website database on a private internal Docker network, exercises Chromium and
  real page/asset preparation, and proves readiness failures. All database keys
  and runtime identities in this fixture are inert, and no ports are published.
- `scripts/test-downstream-helpers.sh <image-reference>` checks downstream
  Python sync and external addon fetch behavior, including legacy compatibility,
  strict partial and layered locks, stale and incomplete lock failures, owned
  workspace enforcement, exact external source handling, conflict isolation,
  final environment checks, and deterministic evidence.

## Security Notes

- Do not add credentials or access tokens in this repo.
- APT verifies PostgreSQL's armored signing key directly. GnuPG key-management
  tools are omitted; `gpgv` remains available for signature verification.
- certifi is a transitive Requests dependency, resolved during the base build.
  Rebuild the base to pick up its patched release; enterprise images inherit
  `/venv` without reinstalling Python dependencies. See the
  [CI release model](#ci-release-model) for scan and downstream publish gates.
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

Publishing jobs share GitHub's native queued concurrency group through alias
promotion and downstream dispatch. Each candidate records the workflow run
number in an OCI `io.github.cbusillo.build-run-number` annotation. Promotion
reads each alias directly from the registry and compares each family
independently. A newer stable or nightly alias on either target protects the
whole pair; unrelated families do not suppress one another. Both eligible
mappings are planned before writes and verified before dispatch. A failed-jobs
retry can repair a partial pair using the already checked candidates from
successful jobs, including candidates from different attempts of the same run.
The source resolver fixes the nightly date for both legs and failed-job retries.
Local verification, publication and proof-download outputs are isolated by run
and attempt; artifact names remain stable so failed-job retries can reuse
successful jobs from earlier attempts of the same run.
Floating and dated nightly aliases have independent pairs. Equal run numbers are
repairs, not supersession. Unique build aliases are independent of shared source
aliases. Community dispatch flags reflect only the families actually delivered
by that execution.

Existing images without that annotation are the legacy baseline (order zero).
Only an exact registry tag-not-found result means an absent alias. Present but
invalid annotations and other registry errors stop promotion. Before deploying
this workflow, allow older publishing executions to finish; rerunning workflows
from before this migration is not supported. Use the current workflow for
subsequent builds.

Run-number ordering belongs to this workflow identity. Keep `build.yml` in place
when reorganizing jobs; recreating it requires an ordering migration before
publication. Superseded families are visible as Actions notices.

## Native Community 20 valuation checkpoints

The Community 20 source build applies
[`20.0-valuation-checkpoints.patch`](patches/20.0-valuation-checkpoints.patch)
inside `stock_account` and its historical stock quantity reader. Odoo 19 source
is unchanged. The build fails if upstream source no longer accepts the patch; qualify the native tests before refreshing the patch.
The extension belongs to the runtime, so removing an importer cannot remove its
valuation semantics. It contains no tenant costing rules or replacement AVCO loop.

For average-cost products, a manual `product.value` can set `after_move_id` and its unit `value`. For example:

```python
env["product.value"].create({"after_move_id": completed_move.id, "value": unit_cost})
```

The anchor supplies the product, company and exact date. The checkpoint applies
after that move and before a greater-ID move at the same timestamp. It is separate
from `move_id`, which overrides an incoming move's total value. Native current valued quantities, with batched backout of moves after each
position, retain company, owner and lot filtering; `lot_id` selects a lot-specific
cost checkpoint. No dates, outgoing values or source chronology are rewritten.

Unanchored manual costs retain native behavior, including applying before
equal-timestamp moves during correction. Only anchored costs use after-move
positioning.
At a shared timestamp, an anchored checkpoint wins the historical seed over
an ordinary manual cost, matching correction history's ordinary-before-moves
placement. Ordinary cost edits at positioned products refresh the same native
valuation. Multiple checkpoints at one position use their record IDs as the
final ordering.
Checkpoint edits/deletions and anchor date/state changes replay native valuation;
anchor dates remain synchronized. Clear a checkpoint before deleting its anchor.
Changing a product's cost method uses the existing native revaluation path;
the positioned AVCO behavior resumes if the product returns to average cost.
Assign an anchor only after it is completed. Changing an anchor's product or
company requires removing its checkpoint first. Assign new positions only to average-cost products, with a lot only when
lot valuation is enabled. Global positions on lot-valuated products refresh all
of their lots. Create multiple checkpoints in one batch when replaying products.
A native reset/revalidation moves the anchor and checkpoint to the new validation
time. Native move-line dates retain their existing separate behavior. Historical
replay follows the existing native accounting path; this is not a new closing
or authorization policy.
This interface does not qualify nonmonotonic source chronology or timezone folds.

The patch bumps the native `stock_account` manifest version. Existing 20 databases
must run the normal gated `stock_account` module update when adopting this runtime;
include it in the tenant's resolved update list before starting the candidate.
An image replacement alone does not create the new field. Fresh foundations
install the schema normally. Tenant owners keep their existing update, backup,
release and acceptance gates; this source PR performs no lane update.

`bash scripts/smoke-valuation.sh <runtime-image>` first creates a synthetic database
with the patch reversed, then upgrades `stock_account` on the patched image and
proves existing unanchored costs/dates survive. It installs a disposable test
addon on a private internal network, runs the native valuation regression class
and checkpoint tests, and removes its containers/network. Odoo 19 skips this
20-specific extension. Supply an optional second argument of native test tags for
a focused local run, for example `/valuation_checkpoint_tests`. CI runs the full
script for each runtime image. The fixtures use synthetic stock and no host ports.
