#!/usr/bin/env bash
set -euo pipefail

image_reference="${1:?Usage: scripts/smoke-readiness.sh <runtime-image> [browser-image]}"
browser_image="${2:-ghcr.io/cbusillo/odoo-docker:19.0-devtools}"
suffix="${RANDOM}-$$"
network="odd-readiness-${suffix}"
postgres="${network}-postgres"
candidate="${network}-candidate"
fixture_dir="$(mktemp -d "${TMPDIR:-/tmp}/odd-readiness.XXXXXX")"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"

cleanup() {
	local exit_status="$?"
	if [[ "${exit_status}" != 0 ]] && docker inspect "${candidate}" >/dev/null 2>&1; then
		docker logs "${candidate}" >&2 || true
	fi
    docker rm -f "${candidate}" "${postgres}" >/dev/null 2>&1 || true
    docker network rm "${network}" >/dev/null 2>&1 || true
    rm -rf "${fixture_dir}"
}
trap cleanup EXIT
chmod 777 "${fixture_dir}"
mkdir "${fixture_dir}/data"
chmod 777 "${fixture_dir}/data"
# Install only the test driver into disposable mounted storage, before it can
# reach a candidate. Chromium itself is supplied by the existing devtools image.
uv export --project "${repo_root}" --frozen --only-group dev --no-hashes --no-emit-project > "${fixture_dir}/browser-requirements.txt"
docker run --rm -v "${fixture_dir}:/fixture" --entrypoint uv "${browser_image}" \
    pip install --target /fixture/browser-driver -r /fixture/browser-requirements.txt >/dev/null
docker network create --internal "${network}" >/dev/null
docker run -d --name "${postgres}" --network "${network}" \
    -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=isolated-fixture \
    ghcr.io/baosystems/postgis:17-3.5 >/dev/null
for _ in {1..60}; do
    if docker exec "${postgres}" pg_isready -h 127.0.0.1 -U odoo >/dev/null 2>&1; then break; fi
    sleep 1
done
docker exec "${postgres}" pg_isready -h 127.0.0.1 -U odoo >/dev/null
common=(--network "${network}" -e ODOO_DB_HOST="${postgres}" -e ODOO_DB_USER=odoo
    -e ODOO_DB_PASSWORD=isolated-fixture -v "${fixture_dir}:/fixture"
    -v "${fixture_dir}/data:/volumes/data"
    -v "${repo_root}/scripts/fixtures/readiness:/fixture-scripts:ro")
docker run --rm "${common[@]}" "${image_reference}" \
    odoo-bin -d readiness_fixture --init website --without-demo --stop-after-init --max-cron-threads=0 --log-level=warn
docker run --rm -i "${common[@]}" "${image_reference}" \
    odoo-bin shell -d readiness_fixture --db_host="${postgres}" --db_user=odoo \
    --db_password=isolated-fixture --max-cron-threads=0 --log-level=warn < "${repo_root}/scripts/fixtures/readiness/setup.py"
identity="$(jq -c '.runtime_identity' "${fixture_dir}/contract.json")"
docker run -d --name "${candidate}" --network-alias fixture.invalid "${common[@]}" \
    -e LAUNCHPLANE_RUNTIME_IDENTITY_JSON="${identity}" "${image_reference}" \
    odoo-bin -d readiness_fixture --db-filter='^readiness_fixture$' --max-cron-threads=0 --log-level=warn >/dev/null
for _ in {1..90}; do
    if docker exec "${candidate}" curl -fsS http://127.0.0.1:8069/launchplane/health >/dev/null 2>&1; then break; fi
    sleep 1
done
docker exec "${candidate}" /venv/bin/python /fixture-scripts/assets.py
docker run --rm "${common[@]}" -e PYTHONPATH=/fixture/browser-driver --entrypoint /venv/bin/python "${browser_image}" \
    /fixture-scripts/browser.py
docker exec "${candidate}" /venv/bin/python /fixture-scripts/check.py
docker exec -i "${candidate}" odoo-bin shell -d readiness_fixture \
    --db_host="${postgres}" --db_user=odoo --db_password=isolated-fixture \
    --max-cron-threads=0 --log-level=warn < "${repo_root}/scripts/fixtures/readiness/registry.py"

# Database state faults, observed by the running registry rather than a mock.
for fault in install update removal stale-update partial-update; do
    docker exec "${candidate}" /usr/local/bin/launchplane-readiness check \
        --contract /fixture/contract.json --evidence /fixture/observations.json >/dev/null
    case "${fault}" in
        install) sql="UPDATE ir_module_module SET state='to install' WHERE name='website';" ;;
        update) sql="UPDATE ir_module_module SET state='to upgrade' WHERE name='website';" ;;
        removal) sql="UPDATE ir_module_module SET state='to remove' WHERE name='website';" ;;
        stale-update) sql="UPDATE ir_config_parameter SET value='{}' WHERE key='launchplane.readiness.release';" ;;
        partial-update) sql="INSERT INTO ir_config_parameter(key,value) VALUES ('base.partially_updated_database','true');" ;;
    esac
    docker exec "${postgres}" psql -U odoo -d readiness_fixture -c "${sql}" >/dev/null
    if docker exec "${candidate}" /usr/local/bin/launchplane-readiness check \
        --contract /fixture/contract.json --evidence /fixture/observations.json >/dev/null 2>&1; then
        echo "Readiness admitted ${fault}" >&2
        exit 1
    fi
    echo "readiness rejected database fault: ${fault}"
    if docker exec "${candidate}" /usr/local/bin/launchplane-readiness prepare \
        --contract /fixture/contract.json --evidence /fixture/browser.json >/dev/null 2>&1; then
        echo "Preparation admitted ${fault}" >&2
        exit 1
    fi
    case "${fault}" in
        install|update|removal) restore="UPDATE ir_module_module SET state='installed' WHERE name='website';" ;;
        stale-update) restore="UPDATE ir_config_parameter SET value=\$receipt\$$(jq -c '.release' "${fixture_dir}/contract.json")\$receipt\$ WHERE key='launchplane.readiness.release';" ;;
        partial-update) restore="DELETE FROM ir_config_parameter WHERE key='base.partially_updated_database';" ;;
    esac
    docker exec "${postgres}" psql -U odoo -d readiness_fixture -c "${restore}" >/dev/null
done
# The readiness surface is unavailable from a private-network peer as well as public proxies.
status="$(docker run --rm --network "${network}" curlimages/curl:8.16.0 \
    -s -o /dev/null -w '%{http_code}' -X POST "http://${candidate}:8069/launchplane/readiness" \
    -H 'Host: fixture.invalid:8069' -H 'X-Odoo-Database: readiness_fixture' -H 'Content-Type: application/json' -d '{}')"
test "${status}" = 403
echo "isolated database-bound readiness smoke passed"
