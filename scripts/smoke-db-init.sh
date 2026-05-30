#!/usr/bin/env bash
set -euo pipefail

image_reference="${1:?Usage: scripts/smoke-db-init.sh <image-reference>}"

suffix="${RANDOM:-0}-$$"
network_name="odoo-db-smoke-${suffix}"
postgres_container="${network_name}-postgres"
odoo_container="${network_name}-odoo"
db_name="odoo_smoke_${suffix//[^[:alnum:]]/_}"
health_db_name="odoo_health_${suffix//[^[:alnum:]]/_}"
db_user="odoo"
db_password="odoo"

cleanup() {
	docker rm -f "${odoo_container}" >/dev/null 2>&1 || true
	docker rm -f "${postgres_container}" >/dev/null 2>&1 || true
	docker network rm "${network_name}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "${network_name}" >/dev/null

docker run -d \
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
odoo-bin \
  -d '${db_name}' \
  --init base \
  --without-demo \
  --http-interface=127.0.0.1 \
  --stop-after-init \
  --log-level=warn
"

base_state="$(docker exec "${postgres_container}" psql -U "${db_user}" -d "${db_name}" -Atc "select state from ir_module_module where name = 'base';")"
if [[ "${base_state}" != "installed" ]]; then
	echo "Expected Odoo base module to be installed, got: ${base_state}" >&2
	exit 1
fi

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
