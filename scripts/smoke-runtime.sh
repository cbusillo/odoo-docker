#!/usr/bin/env bash
set -euo pipefail

image_reference="${1:?Usage: scripts/smoke-runtime.sh <image-reference>}"

docker run --rm --entrypoint /bin/bash "${image_reference}" -lc '
set -euo pipefail
test -x /odoo/odoo-bin
test -x /usr/local/bin/uv
test -x /usr/local/bin/odoo-python-sync.sh
test -x /usr/local/bin/odoo-fetch-addons.sh
test -x /usr/bin/pg_restore
test -d /venv
test -d /opt/project
test -d /opt/project/addons
test -d /opt/extra_addons
test -d /opt/launchplane/addons
test -f /opt/launchplane/addons/launchplane_runtime_health/__manifest__.py
test -f /volumes/config/_generated.conf
/venv/bin/python -c "import sys; assert sys.version_info[:2] == (3, 13), sys.version"
/venv/bin/python -c "import cryptography, OpenSSL"
test ! -e /venv/bin/pip
/venv/bin/python -c "import importlib.util; assert importlib.util.find_spec(\"pip\") is None"
/odoo/odoo-bin --help >/dev/null
/odoo/odoo-bin shell --help >/dev/null
ODOO_SOURCE_BIN=/bin/true ODOO_WRAPPER_PYTHON=/bin/echo /odoo/odoo-bin --stop-after-init | grep -F -- "--load=base,web,launchplane_runtime_health" >/dev/null
'

echo "runtime smoke checks passed: ${image_reference}"
