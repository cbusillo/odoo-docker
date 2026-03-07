# AGENTS.md

Use this file as the quick-start guide for coding agents working in this repo.
Keep it short; defer deeper detail to the README and scripts.

## Start Here

- Read [README.md](README.md) before changing build or release behavior.
- Treat this repo as the public base image contract for downstream Odoo images.
- Preserve `/venv`, `/opt/project`, `/opt/project/addons`, and
  `/opt/extra_addons` as stable downstream layout guarantees.

## Primary Commands

- Build runtime image: `docker build -t ghcr.io/cbusillo/odoo-docker:19.0-runtime --target runtime .`
- Build devtools image: `docker build -t ghcr.io/cbusillo/odoo-docker:19.0-devtools --target runtime-devtools .`
- Smoke runtime image: `bash scripts/smoke-runtime.sh <image-reference>`
- Smoke devtools image: `bash scripts/smoke-devtools.sh <image-reference>`
- Validate downstream helper contract: `bash scripts/test-downstream-helpers.sh <image-reference>`

## Release Gate

- Before release-oriented changes, run JetBrains inspections with the PyCharm
  inspection tool on changed scope and then whole-project scope.
- Treat local image validation as part of the gate: build the affected target,
  run the matching smoke script, and run `scripts/test-downstream-helpers.sh`
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

- Keep the image contract generic. Project-specific policy belongs downstream.
- Prefer small, reviewable Dockerfile and script changes.
- When changing helper scripts under `scripts/`, keep the downstream contract in
  sync with README wording and validation coverage.

## Cleanup Hygiene

- After local validation, remove ad hoc local verification artifacts you created
  if they are no longer needed, especially one-off image tags and stopped test
  containers.
- Do not run broad destructive cleanup like `docker system prune` unless the
  operator explicitly asks for it.
