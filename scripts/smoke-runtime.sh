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
test ! -e /usr/share/python-wheels
/usr/bin/python3 -c "import importlib.util; assert importlib.util.find_spec(\"pip\") is None; assert importlib.util.find_spec(\"ensurepip\") is None"
base_python="$(/venv/bin/python -c "import sys; print(sys._base_executable)")"
case "${base_python}" in /opt/uv/python/*) ;; *) echo "Unexpected base interpreter: ${base_python}" >&2; exit 1 ;; esac
base_site_packages="$(env -u VIRTUAL_ENV "${base_python}" -c "import sysconfig; print(sysconfig.get_path(\"purelib\"))")"
base_scripts="$(env -u VIRTUAL_ENV "${base_python}" -c "import sysconfig; print(sysconfig.get_path(\"scripts\"))")"
base_stdlib="$(env -u VIRTUAL_ENV "${base_python}" -c "import sysconfig; print(sysconfig.get_path(\"stdlib\"))")"
for managed_path in "${base_site_packages}" "${base_scripts}" "${base_stdlib}"; do
  case "${managed_path}" in /opt/uv/python/*) ;; *) echo "Unexpected managed Python path: ${managed_path}" >&2; exit 1 ;; esac
  test -d "${managed_path}"
done
test -z "$(find "${base_site_packages}" -maxdepth 1 -name "pip-*.dist-info" -print -quit)"
test -z "$(find "${base_scripts}" -maxdepth 1 -name "pip*" -print -quit)"
test ! -e "${base_stdlib}/ensurepip"
env -u VIRTUAL_ENV "${base_python}" -c "import importlib.util; assert importlib.util.find_spec(\"pip\") is None; assert importlib.util.find_spec(\"ensurepip\") is None"
/odoo/odoo-bin --help >/dev/null
/odoo/odoo-bin shell --help >/dev/null
ODOO_SOURCE_BIN=/bin/true ODOO_WRAPPER_PYTHON=/bin/echo /odoo/odoo-bin --stop-after-init | grep -F -- "--load=base,web,launchplane_runtime_health" >/dev/null
'

echo "runtime smoke checks passed: ${image_reference}"
