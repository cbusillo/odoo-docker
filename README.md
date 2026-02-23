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

## Source Pinning

The workflow resolves the current `odoo/odoo` `19.0` commit and pins that exact
revision into the build. This gives repeatable artifacts per run and makes
nightly updates explicit.

## Build

```bash
docker build -t ghcr.io/cbusillo/odoo-docker:19.0-runtime --target runtime .
docker build -t ghcr.io/cbusillo/odoo-docker:19.0-devtools --target runtime-devtools .
```

## Security Notes

- Do not add credentials or access tokens in this repo.
- Proprietary addons should be fetched by downstream builds using BuildKit
  secrets.
