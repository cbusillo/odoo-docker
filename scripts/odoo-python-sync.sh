#!/usr/bin/env bash
set -euo pipefail

readonly sync_mode="${1:-}"
readonly project_root="/opt/project"
readonly root_pyproject_path="${project_root}/pyproject.toml"
readonly root_lock_path="${project_root}/uv.lock"
readonly addon_roots=(/opt/project/addons /opt/extra_addons)
readonly python_executable="/venv/bin/python3"
readonly skipped_addons_raw="${ODOO_PYTHON_SYNC_SKIP_ADDONS:-}"
skipped_addons=",$(printf '%s' "${skipped_addons_raw}" | tr -d '[:space:]'),"
readonly skipped_addons

if [[ "${sync_mode}" != "prod" && "${sync_mode}" != "dev" ]]; then
	echo "Usage: odoo-python-sync.sh <prod|dev>" >&2
	exit 1
fi

ensure_base_environment() {
	if [[ ! -x "${python_executable}" ]]; then
		echo "Missing inherited ${python_executable}; base image must provide /venv." >&2
		exit 1
	fi

	if [[ -n "${VIRTUAL_ENV:-}" && "${VIRTUAL_ENV}" != "/venv" ]]; then
		echo "Unsupported VIRTUAL_ENV=${VIRTUAL_ENV}; downstream images must inherit /venv." >&2
		exit 1
	fi

	if [[ -n "${UV_PROJECT_ENVIRONMENT:-}" && "${UV_PROJECT_ENVIRONMENT}" != "/venv" ]]; then
		echo "Unsupported UV_PROJECT_ENVIRONMENT=${UV_PROJECT_ENVIRONMENT}; downstream images must inherit /venv." >&2
		exit 1
	fi

	export VIRTUAL_ENV=/venv
	export UV_PROJECT_ENVIRONMENT=/venv
}

ensure_project_layout() {
	if [[ ! -e "${project_root}" ]]; then
		echo "Missing ${project_root}; downstream images must populate /opt/project." >&2
		exit 1
	fi

	if [[ ! -d "/opt/project/addons" ]]; then
		echo "Missing /opt/project/addons; downstream images must keep the PyCharm-first addon layout." >&2
		exit 1
	fi

	if [[ ! -d "/opt/extra_addons" ]]; then
		echo "Missing /opt/extra_addons; base addon layout is broken." >&2
		exit 1
	fi

	if [[ -e "${root_pyproject_path}" && ! -e "${root_lock_path}" ]]; then
		echo "Found ${root_pyproject_path} without ${root_lock_path}; root project installs must stay lockfile-backed." >&2
		exit 1
	fi

	if [[ -e "${root_lock_path}" && ! -e "${root_pyproject_path}" ]]; then
		echo "Found ${root_lock_path} without ${root_pyproject_path}; root project metadata is incomplete." >&2
		exit 1
	fi
}

python_has_optional_dependency() {
	local pyproject_path="$1"
	local extra_name="$2"

	PYPROJECT_PATH="${pyproject_path}" EXTRA_NAME="${extra_name}" "${python_executable}" - <<'PY'
import os
from pathlib import Path
import tomllib

path = Path(os.environ["PYPROJECT_PATH"])
if not path.exists():
    raise SystemExit(1)

data = tomllib.loads(path.read_text(encoding="utf-8"))
optional = data.get("project", {}).get("optional-dependencies", {}) or {}
raise SystemExit(0 if os.environ["EXTRA_NAME"] in optional else 1)
PY
}

addon_is_skipped() {
	local addon_name="$1"
	[[ "${skipped_addons}" == *,"${addon_name}",* ]]
}

install_root_dependencies() {
	if [[ ! -f "${root_pyproject_path}" ]]; then
		return
	fi

	local requirements_file
	requirements_file="$(mktemp /tmp/odoo-python-sync-root-XXXXXX.txt)"
	local export_args=(--frozen --format requirements.txt --no-emit-project --output-file "${requirements_file}")

	if [[ "${sync_mode}" == "dev" ]] && python_has_optional_dependency "${root_pyproject_path}" dev; then
		export_args+=(--extra dev)
	fi

	echo "Installing root project dependencies from ${root_lock_path} into /venv..."
	(
		cd "${project_root}"
		uv export "${export_args[@]}"
	)

	if [[ -s "${requirements_file}" ]]; then
		uv pip install --python /venv/bin/python -r "${requirements_file}"
	fi

	rm -f "${requirements_file}"
}

install_addon_dependencies() {
	local addon_root="$1"
	local addon_path

	if [[ ! -d "${addon_root}" ]]; then
		return
	fi

	shopt -s nullglob
	for addon_path in "${addon_root}"/*; do
		[[ -d "${addon_path}" ]] || continue
		if addon_is_skipped "$(basename "${addon_path}")"; then
			echo "Skipping addon Python sync for $(basename "${addon_path}")"
			continue
		fi

		if [[ -f "${addon_path}/requirements.txt" ]]; then
			echo "Installing addon requirements: ${addon_path}/requirements.txt"
			uv pip install --python /venv/bin/python -r "${addon_path}/requirements.txt"
		fi

		if [[ -f "${addon_path}/pyproject.toml" ]]; then
			echo "Installing addon package from ${addon_path}/pyproject.toml"
			(
				cd "${addon_path}"
				if [[ "${sync_mode}" == "dev" ]] && python_has_optional_dependency "${addon_path}/pyproject.toml" dev; then
					uv pip install --python /venv/bin/python '.[dev]'
				else
					uv pip install --python /venv/bin/python .
				fi
			)
		fi

		if [[ "${sync_mode}" == "dev" && -f "${addon_path}/requirements-dev.txt" ]]; then
			echo "Installing addon dev requirements: ${addon_path}/requirements-dev.txt"
			uv pip install --python /venv/bin/python -r "${addon_path}/requirements-dev.txt"
		fi
	done
	shopt -u nullglob
}

ensure_base_environment
ensure_project_layout
install_root_dependencies

for addon_root in "${addon_roots[@]}"; do
	install_addon_dependencies "${addon_root}"
done

echo "odoo-python-sync completed in ${sync_mode} mode"
