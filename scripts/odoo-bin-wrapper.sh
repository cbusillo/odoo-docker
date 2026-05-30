#!/usr/bin/env bash
set -euo pipefail

odoo_script="${ODOO_SOURCE_BIN:-/odoo/odoo-bin.source}"
python_executable="${ODOO_WRAPPER_PYTHON:-/venv/bin/python}"
arguments=("$@")
inject_runtime_defaults=true
launchplane_addons_path="/opt/launchplane/addons"
launchplane_runtime_module="launchplane_runtime_health"
default_odoo_addons_path="/opt/project/addons,/opt/extra_addons,/opt/launchplane/addons,/odoo/addons,/odoo/odoo/addons"
default_server_wide_modules="base,web"

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

normalize_comma_list_with_first_item() {
  local first_item="$1"
  local raw_list="$2"
  local normalized_items=("$first_item")
  local item trimmed existing

  IFS=',' read -ra raw_items <<<"$raw_list"
  for item in "${raw_items[@]}"; do
    trimmed="${item//[[:space:]]/}"
    if [[ -z "$trimmed" ]] || [[ "$trimmed" == "$first_item" ]]; then
      continue
    fi
    for existing in "${normalized_items[@]}"; do
      if [[ "$existing" == "$trimmed" ]]; then
        continue 2
      fi
    done
    normalized_items+=("$trimmed")
  done

  local IFS=','
  printf '%s' "${normalized_items[*]}"
}

normalize_server_wide_modules() {
  local raw_list="$1"
  local normalized_items=("base" "web")
  local item trimmed existing

  IFS=',' read -ra raw_items <<<"$raw_list"
  for item in "${raw_items[@]}"; do
    trimmed="${item//[[:space:]]/}"
    if [[ -z "$trimmed" ]] || [[ "$trimmed" == "base" ]] || [[ "$trimmed" == "web" ]] || [[ "$trimmed" == "$launchplane_runtime_module" ]]; then
      continue
    fi
    for existing in "${normalized_items[@]}"; do
      if [[ "$existing" == "$trimmed" ]]; then
        continue 2
      fi
    done
    normalized_items+=("$trimmed")
  done
  normalized_items+=("$launchplane_runtime_module")

  local IFS=','
  printf '%s' "${normalized_items[*]}"
}

ensure_option_value() {
  local option_name="$1"
  local default_value="$2"
  local normalizer="$3"
  local updated_arguments=()
  local found=false
  local argument value normalized_value
  local index=0

  while [[ "$index" -lt "${#arguments[@]}" ]]; do
    argument="${arguments[$index]}"
    if [[ "$argument" == "$option_name="* ]]; then
      value="${argument#*=}"
      normalized_value="$($normalizer "$value")"
      updated_arguments+=("$option_name=$normalized_value")
      found=true
    elif [[ "$argument" == "$option_name" ]]; then
      value="${arguments[$((index + 1))]:-}"
      normalized_value="$($normalizer "$value")"
      updated_arguments+=("$option_name" "$normalized_value")
      found=true
      index=$((index + 1))
    else
      updated_arguments+=("$argument")
    fi
    index=$((index + 1))
  done

  if [[ "$found" != "true" ]]; then
    normalized_value="$($normalizer "$default_value")"
    arguments=("$option_name=$normalized_value" "${updated_arguments[@]}")
  else
    arguments=("${updated_arguments[@]}")
  fi
}

normalize_addons_path() {
  normalize_comma_list_with_first_item "$launchplane_addons_path" "$1"
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

  ensure_option_value "--addons-path" "${ODOO_ADDONS_PATH:-$default_odoo_addons_path}" normalize_addons_path
  ensure_option_value "--load" "${ODOO_SERVER_WIDE_MODULES:-$default_server_wide_modules}" normalize_server_wide_modules

  if [[ -n "${ODOO_DATA_DIR:-}" ]] && ! argument_present "--data-dir"; then
    arguments=("--data-dir=${ODOO_DATA_DIR}" "${arguments[@]}")
  fi
fi

exec "$python_executable" "$odoo_script" "${arguments[@]}"
