#!/usr/bin/env bash
set -euo pipefail

image_reference="${1:?Usage: scripts/smoke-runtime.sh <image-reference>}"

docker run --rm --entrypoint /bin/bash "${image_reference}" -lc '
set -euo pipefail
test -x /odoo/odoo-bin
test -x /usr/local/bin/uv
test -x /usr/bin/pg_restore
test -d /venv
test -f /volumes/config/_generated.conf
/venv/bin/python -c "import sys; assert sys.version_info[:2] == (3, 13), sys.version"
/odoo/odoo-bin --help >/dev/null
/odoo/odoo-bin shell --help >/dev/null
'

echo "runtime smoke checks passed: ${image_reference}"
