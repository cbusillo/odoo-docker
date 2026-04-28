#!/usr/bin/env bash
set -euo pipefail

image_reference="${1:?Usage: scripts/smoke-db-init.sh <image-reference>}"

suffix="${RANDOM:-0}-$$"
network_name="odoo-db-smoke-${suffix}"
postgres_container="${network_name}-postgres"
db_name="odoo_smoke_${suffix//[^[:alnum:]]/_}"
db_user="odoo"
db_password="odoo"

cleanup() {
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
	postgres:17-alpine >/dev/null

ready=false
for _ in {1..60}; do
	if docker exec "${postgres_container}" pg_isready -U "${db_user}" -d postgres >/dev/null 2>&1; then
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

echo "database-backed smoke checks passed: ${image_reference}"
