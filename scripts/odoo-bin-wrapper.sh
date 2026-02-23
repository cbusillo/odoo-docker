#!/usr/bin/env bash
set -euo pipefail

odoo_script="${ODOO_SOURCE_BIN:-/usr/local/bin/odoo-source-bin}"
python_executable="${ODOO_WRAPPER_PYTHON:-/venv/bin/python}"
arguments=("$@")
inject_runtime_defaults=true

# Keep subcommand semantics intact. Odoo 19 expects the subcommand as the
# first token when present (for example: `odoo-bin shell ...`).
first_argument="${arguments[0]:-}"
case "$first_argument" in
  server|start|""|-*)
    inject_runtime_defaults=true
    ;;
  *)
    inject_runtime_defaults=false
    ;;
esac

if [[ ! -x "$odoo_script" ]]; then
  echo "Missing Odoo executable at ${odoo_script}" >&2
  exit 1
fi

argument_present() {
  local option_name="$1"
  local argument
  for argument in "${arguments[@]}"; do
    if [[ "$argument" == "$option_name" ]] || [[ "$argument" == "$option_name="* ]]; then
      return 0
    fi
  done
  return 1
}

if [[ "$inject_runtime_defaults" == "true" ]]; then
  if ! argument_present "-c" && ! argument_present "--config"; then
    if [[ -f "/volumes/config/_generated.conf" ]]; then
      arguments=("-c" "/volumes/config/_generated.conf" "${arguments[@]}")
    elif [[ -f "/etc/odoo/odoo.conf" ]]; then
      arguments=("-c" "/etc/odoo/odoo.conf" "${arguments[@]}")
    fi
  fi
  if [[ -n "${ODOO_DB_HOST:-}" ]] && ! argument_present "--db_host"; then
    arguments=("--db_host=${ODOO_DB_HOST}" "${arguments[@]}")
  fi

  if [[ -n "${ODOO_DB_PORT:-}" ]] && ! argument_present "--db_port"; then
    arguments=("--db_port=${ODOO_DB_PORT}" "${arguments[@]}")
  fi

  if [[ -n "${ODOO_DB_USER:-}" ]] && ! argument_present "--db_user"; then
    arguments=("--db_user=${ODOO_DB_USER}" "${arguments[@]}")
  fi

  if [[ -n "${ODOO_DB_PASSWORD:-}" ]] && ! argument_present "--db_password"; then
    arguments=("--db_password=${ODOO_DB_PASSWORD}" "${arguments[@]}")
  fi

  if [[ -n "${ODOO_ADDONS_PATH:-}" ]] && ! argument_present "--addons-path"; then
    arguments=("--addons-path=${ODOO_ADDONS_PATH}" "${arguments[@]}")
  fi

  if [[ -n "${ODOO_DATA_DIR:-}" ]] && ! argument_present "--data-dir"; then
    arguments=("--data-dir=${ODOO_DATA_DIR}" "${arguments[@]}")
  fi
fi

exec "$python_executable" "$odoo_script" "${arguments[@]}"
