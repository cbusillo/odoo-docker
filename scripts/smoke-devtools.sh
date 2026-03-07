#!/usr/bin/env bash
set -euo pipefail

image_reference="${1:?Usage: scripts/smoke-devtools.sh <image-reference>}"

docker run --rm --entrypoint /bin/bash "${image_reference}" -lc '
set -euo pipefail
binary="${CHROME_BIN:-/usr/bin/chromium}"
if [[ ! -x "${binary}" ]]; then
  if [[ -x /usr/bin/chromium-browser ]]; then
    binary="/usr/bin/chromium-browser"
  else
    echo "Chromium binary not found" >&2
    exit 1
  fi
fi
"${binary}" --version >/dev/null
/venv/bin/python3 - <<"PY"
from pathlib import Path
import site

pth_path = Path(site.getsitepackages()[0]) / "odoo_paths.pth"
contents = pth_path.read_text(encoding="utf-8").splitlines()
assert contents == [
    "/odoo",
    "/opt/project/addons",
    "/opt/extra_addons",
], contents
PY
'

echo "devtools smoke checks passed: ${image_reference}"
