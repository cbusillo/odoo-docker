#!/usr/bin/env bash
set -euo pipefail

wrapper_path="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/odoo-bin-wrapper.sh"

test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT

fake_source_bin="${test_root}/fake-odoo-source-bin"
fake_python_bin="${test_root}/fake-python"
captured_arguments_file="${test_root}/captured-arguments.txt"

cat > "${fake_source_bin}" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${fake_source_bin}"

cat > "${fake_python_bin}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" > "${WRAPPER_CAPTURE_FILE:?missing WRAPPER_CAPTURE_FILE}"
exit 0
EOF
chmod +x "${fake_python_bin}"

run_wrapper() {
  WRAPPER_CAPTURE_FILE="${captured_arguments_file}" \
  ODOO_SOURCE_BIN="${fake_source_bin}" \
  ODOO_WRAPPER_PYTHON="${fake_python_bin}" \
  ODOO_DB_HOST="database" \
  ODOO_DB_PORT="5432" \
  ODOO_DB_USER="odoo" \
  ODOO_DB_PASSWORD="secret" \
  ODOO_ADDONS_PATH="/opt/project/addons" \
  ODOO_DATA_DIR="/volumes/data" \
  "${wrapper_path}" "$@"
}

run_wrapper --stop-after-init
grep -Fx -- "${fake_source_bin}" "${captured_arguments_file}" >/dev/null
grep -Fx -- "--db_host=database" "${captured_arguments_file}" >/dev/null
grep -Fx -- "--db_port=5432" "${captured_arguments_file}" >/dev/null
grep -Fx -- "--db_user=odoo" "${captured_arguments_file}" >/dev/null
grep -Fx -- "--db_password=secret" "${captured_arguments_file}" >/dev/null
grep -Fx -- "--addons-path=/opt/project/addons" "${captured_arguments_file}" >/dev/null

run_wrapper shell -d opw --no-http
second_argument="$(sed -n '2p' "${captured_arguments_file}")"
if [[ "${second_argument}" != "shell" ]]; then
  echo "Expected shell subcommand as second argument, got: ${second_argument}" >&2
  exit 1
fi
if grep -q -- "--db_host=" "${captured_arguments_file}"; then
  echo "Wrapper injected server defaults for shell subcommand" >&2
  exit 1
fi

run_wrapper -d shell --stop-after-init
if ! grep -q -- "--db_host=database" "${captured_arguments_file}"; then
  echo "Wrapper failed to inject server defaults when database name is shell" >&2
  exit 1
fi

echo "odoo-bin wrapper tests passed"
