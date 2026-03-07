#!/usr/bin/env bash
set -euo pipefail

if [ ! -x /venv/bin/python3 ]; then
  echo "Skipping dev addon path setup; /venv/bin/python3 missing."
  exit 0
fi

/venv/bin/python3 - <<'PY'
from pathlib import Path
import site

paths = [
    "/odoo",
    "/opt/project/addons",
    "/opt/extra_addons",
]

site_packages = Path(site.getsitepackages()[0])
pth_path = site_packages / "odoo_paths.pth"
pth_path.write_text("\n".join(paths) + "\n", encoding="utf-8")
print(f"Updated {pth_path}")
PY
