#!/usr/bin/env bash
set -euo pipefail

image_reference="${1:?Usage: scripts/test-downstream-helpers.sh <image-reference>}"
downstream_helper_repo="${ODOO_DOWNSTREAM_HELPER_REPO:-McMillan-Woods-Global/disable_odoo_online}"
downstream_helper_ref="${ODOO_DOWNSTREAM_HELPER_REF:-c69e4df113df460fb933a3519331fdadbca1a32f}"

test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT

mkdir -p \
	"${test_root}/tools" \
	"${test_root}/addons/test_local_pkg/test_local_pkg" \
	"${test_root}/addons/test_requirements" \
	"${test_root}/addons/test_dev_requirements"

cat >"${test_root}/pyproject.toml" <<'EOF'
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "downstream-root"
version = "0.0.0"
dependencies = [
    "python-slugify==8.0.4",
]

[project.optional-dependencies]
dev = [
    "humanize==4.13.0",
]
EOF

cat >"${test_root}/tools/__init__.py" <<'EOF'
"""Downstream helper test package."""
EOF

cat >"${test_root}/addons/test_local_pkg/pyproject.toml" <<'EOF'
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "test-local-pkg"
version = "0.0.0"
EOF

cat >"${test_root}/addons/test_local_pkg/test_local_pkg/__init__.py" <<'EOF'
VALUE = "local-package-installed"
EOF

chmod -R a+rX "${test_root}"

run_sync_check() {
	local sync_mode="$1"

	docker run --rm \
		-e ODOO_ADDON_REPOSITORIES="${downstream_helper_repo}@${downstream_helper_ref}" \
		-v "${test_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "
set -euo pipefail
odoo-fetch-addons.sh
odoo-python-sync.sh ${sync_mode}
test -d /opt/extra_addons/${downstream_helper_repo##*/}
test -f /opt/extra_addons/${downstream_helper_repo##*/}/__manifest__.py
/venv/bin/python - <<'PY'
import importlib.util
from test_local_pkg import VALUE

assert VALUE == 'local-package-installed'
assert importlib.util.find_spec('slugify') is not None
humanize_spec = importlib.util.find_spec('humanize')

if '${sync_mode}' == 'dev':
    assert humanize_spec is not None
else:
    assert humanize_spec is None
PY
"
}

run_sync_check prod
run_sync_check dev

echo "downstream helper tests passed: ${image_reference}"
