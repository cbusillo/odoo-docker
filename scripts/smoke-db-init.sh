#!/usr/bin/env bash
set -euo pipefail

image_reference="${1:?Usage: scripts/smoke-db-init.sh <image-reference>}"

suffix="${RANDOM:-0}-$$"
network_name="odoo-db-smoke-${suffix}"
postgres_container="${network_name}-postgres"
odoo_container="${network_name}-odoo"
shell_container="${network_name}-shell"
db_name="odoo_smoke_${suffix//[^[:alnum:]]/_}"
health_db_name="odoo_health_${suffix//[^[:alnum:]]/_}"
db_user="odoo"
db_password=$'synthetic smoke password $ # \\ \' " %'

cleanup() {
	docker rm -f "${odoo_container}" >/dev/null 2>&1 || true
	docker rm -f "${shell_container}" >/dev/null 2>&1 || true
	docker rm -f "${postgres_container}" >/dev/null 2>&1 || true
	docker network rm "${network_name}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

assert_password_absent_from_processes() {
	local container="$1"
	local listing
	listing="$(docker top "${container}" -eo pid,args)"
	if [[ "${listing}" == *"${db_password}"* ]] || \
		grep -Eq -- '(^|[[:space:]])(--db_password(=|[[:space:]]|$)|-w([^[:space:]]*|$))' <<<"${listing}"; then
		echo "Database password appeared in disposable-container process arguments" >&2
		return 1
	fi
	printf '%s\n' "${listing}"
}

docker network create "${network_name}" >/dev/null

docker run --rm -d \
	--name "${postgres_container}" \
	--network "${network_name}" \
	-e POSTGRES_DB=postgres \
	-e POSTGRES_USER="${db_user}" \
	-e POSTGRES_PASSWORD="${db_password}" \
	ghcr.io/baosystems/postgis:17-3.5 >/dev/null

ready=false
for _ in {1..60}; do
	if docker run --rm \
		--network "${network_name}" \
		--entrypoint pg_isready \
		ghcr.io/baosystems/postgis:17-3.5 \
		-h "${postgres_container}" \
		-U "${db_user}" \
		-d postgres >/dev/null 2>&1; then
		ready=true
		break
	fi
	sleep 1
done

if [[ "${ready}" != "true" ]]; then
	docker logs "${postgres_container}" >&2 || true
	echo "PostgreSQL did not become ready" >&2
	exit 1
fi

docker run --rm \
	--network "${network_name}" \
	-e ODOO_DB_HOST="${postgres_container}" \
	-e ODOO_DB_PORT=5432 \
	-e ODOO_DB_USER="${db_user}" \
	-e ODOO_DB_PASSWORD="${db_password}" \
	--entrypoint /bin/bash \
	"${image_reference}" -lc "
set -euo pipefail
# Check that the environment password still overrides a stale config value.
printf '[options]\ndb_password = stale-config-password\n' > /volumes/config/_generated.conf
odoo-bin \
  -d '${db_name}' \
  --init base,launchplane_runtime_health \
  --test-tags /base:TestImage,/base:TestIrAttachment.test_10_image_autoresize \
  --without-demo \
  --http-interface=127.0.0.1 \
  --stop-after-init \
  --log-level=warn \
  --log-handler=odoo.tests:INFO 2>&1 | tee /tmp/odoo-image-tests.log
if ! grep -Eq \"odoo[.]tests[.]result: 0 failed, 0 error[(]s[)] of [1-9][0-9]* tests when loading database '${db_name}'[[:space:]]*$\" /tmp/odoo-image-tests.log; then
  echo 'Expected a successful, nonempty upstream Odoo image test run' >&2
  exit 1
fi
"

for module in base launchplane_runtime_health; do
	module_state="$(docker exec "${postgres_container}" psql -U "${db_user}" -d "${db_name}" -Atc "select state from ir_module_module where name = '${module}';")"
	if [[ "${module_state}" != "installed" ]]; then
		echo "Expected Odoo ${module} module to be installed, got: ${module_state}" >&2
		exit 1
	fi
done

# Authenticate a non-server data command through the same image wrapper.
docker run --rm \
	--network "${network_name}" \
	-e PGHOST="${postgres_container}" \
	-e PGPORT=5432 \
	-e PGUSER="${db_user}" \
	-e ODOO_DB_PASSWORD="${db_password}" \
	--entrypoint /bin/bash \
	"${image_reference}" -lc "
set -euo pipefail
odoo-bin db dump '${db_name}' /tmp/smoke.dump --format dump
test -s /tmp/smoke.dump
"

# Keep an authenticated script-runner shell alive for process-list evidence.
docker run -d \
	--name "${shell_container}" \
	--network "${network_name}" \
	-e PGHOST="${postgres_container}" \
	-e PGPORT=5432 \
	-e PGUSER="${db_user}" \
	-e ODOO_DB_PASSWORD="${db_password}" \
	--entrypoint /bin/bash \
	"${image_reference}" -lc "
set -euo pipefail
printf '%s\n' \
  'env.cr.execute(\"SELECT 1\")' \
  'assert env.cr.fetchone() == (1,)' \
  'print(\"authenticated shell ready\", flush=True)' \
  'import time; time.sleep(120)' \
  | odoo-bin shell -d '${db_name}' --no-http --log-level=error
" >/dev/null

shell_ready=false
for _ in {1..60}; do
	if docker logs "${shell_container}" 2>&1 | grep -Fq 'authenticated shell ready'; then
		shell_ready=true
		break
	fi
	sleep 1
done
if [[ "${shell_ready}" != "true" ]]; then
	docker logs "${shell_container}" >&2 || true
	echo "Authenticated Odoo shell did not become ready" >&2
	exit 1
fi
assert_password_absent_from_processes "${shell_container}"

runtime_identity_json="{\"schema_version\":1,\"product\":\"odoo-smoke\",\"context\":\"smoke\",\"instance\":\"testing\",\"artifact_id\":\"artifact-smoke\"}"

docker run -d \
	--name "${odoo_container}" \
	--network "${network_name}" \
	-e ODOO_DB_HOST="${postgres_container}" \
	-e ODOO_DB_PORT=5432 \
	-e ODOO_DB_NAME="${health_db_name}" \
	-e ODOO_DB_USER="${db_user}" \
	-e ODOO_DB_PASSWORD="${db_password}" \
	-e ODOO_MASTER_PASSWORD="safe-master" \
	-e ODOO_ADMIN_PASSWORD="safe-admin" \
	-e LAUNCHPLANE_RUNTIME_IDENTITY_JSON="${runtime_identity_json}" \
	"${image_reference}" \
	odoo-bin --http-interface=0.0.0.0 --log-level=warn >/dev/null

health_ready=false
for _ in {1..90}; do
	if docker run --rm \
		--network "${network_name}" \
		curlimages/curl:8.16.0 \
		-fsS "http://${odoo_container}:8069/launchplane/health" >/dev/null 2>&1; then
		health_ready=true
		break
	fi
	sleep 2
done

if [[ "${health_ready}" != "true" ]]; then
	docker logs "${odoo_container}" >&2 || true
	echo "Launchplane health endpoint did not become ready" >&2
	exit 1
fi

assert_password_absent_from_processes "${odoo_container}"

docker run --rm \
	--network "${network_name}" \
	curlimages/curl:8.16.0 \
	-fsS "http://${odoo_container}:8069/launchplane/health" \
	| python3 -c '
import json
import sys

payload = json.load(sys.stdin)
assert payload["status"] == "pass", payload
runtime_identity = payload["runtime_identity"]
assert runtime_identity["product"] == "odoo-smoke", payload
assert runtime_identity["artifact_id"] == "artifact-smoke", payload
'

echo "database-backed smoke checks passed: ${image_reference}"
