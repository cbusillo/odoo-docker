#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv run --no-project --with pyyaml python "${repo_root}/tests/test_ghcr_retention.py"
