# AGENTS.md

Use this file as the quick-start guide for coding agents working in this repo.
Keep it short; defer deeper detail to the README and scripts.

## Start Here

- Read [README.md](README.md) before changing build or release behavior.
- Treat this repo as the base image contract for Launchplane-managed Odoo
  runtimes. Keep tenant/business policy downstream, but image-owned
  Launchplane runtime substrate belongs here.
- Preserve `/venv`, `/opt/project`, `/opt/project/addons`, and
  `/opt/extra_addons` as stable downstream layout guarantees. Preserve
  `/opt/launchplane/addons` as the image-owned runtime addon root.
- Keep `pip` and `ensurepip` out of the runtime Python installations, including
  `/venv` and the uv-managed interpreter under `/opt/uv/python`. Use the
  image-owned `uv` binary for base and downstream Python dependency installation.

## Workflow Metadata

- Use [`.github/github.json`](.github/github.json)
  for primary commands, validation gates, workflow routing, and cleanup policy.

## Release Gate

- Before release-oriented changes, run JetBrains inspections with the PyCharm
  inspection tool on changed scope and then whole-project scope.
- Treat local image validation as part of the gate: build the affected target,
  run the matching smoke script, run `scripts/smoke-db-init.sh` for runtime
  database initialization coverage, and run `scripts/test-downstream-helpers.sh`
  for runtime changes that affect downstream behavior.
- If a change affects downstream image semantics, verify with a real local
  build instead of reasoning from the Dockerfile alone.

## PyCharm Inspection Notes

- Use the PyCharm inspection tool instead of guessing from editor gutters.
- Current known whole-project false positives are acceptable noise:
  - Dockerfile variable resolution warnings for standard Docker build vars like
    `$BUILDPLATFORM` and `$TARGETPLATFORM`
  - Dockerfile shell-local variable resolution warnings inside `RUN` steps
  - `.dockerignore` `IgnoreCoverEntry` warnings
- Do not churn on those current findings unless you are intentionally changing
  the inspection profile or the affected Dockerfile / `.dockerignore` logic.
- Do not blanket-disable inspections. If tuning is needed, prefer narrow
  file-scope or inspection-scope exceptions.

## Editing Guardrails

- Keep the image contract tenant-agnostic. Project-specific business policy
  belongs downstream; shared Launchplane runtime compatibility belongs in this
  image-owned substrate layer.
- Prefer small, reviewable Dockerfile and script changes.
- When changing helper scripts under `scripts/`, keep the downstream contract in
  sync with README wording and validation coverage.

## Cleanup Hygiene

- After local validation, remove ad hoc local verification artifacts you created
  if they are no longer needed, especially one-off image tags and stopped test
  containers.
- Do not run broad destructive cleanup like `docker system prune` unless the
  operator explicitly asks for it.
