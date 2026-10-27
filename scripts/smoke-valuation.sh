#!/usr/bin/env bash
set -euo pipefail
image="${1:?Usage: scripts/smoke-valuation.sh <runtime-image>}"
test_tags="${2:-/valuation_checkpoint_tests,/stock_account:TestStockValuation}"
fixture="$(cd "$(dirname "$0")/fixtures/valuation" && pwd)"
major="$(docker run --rm --network none --entrypoint /venv/bin/python "$image" -c 'import runpy; print(runpy.run_path("/odoo/odoo/release.py")["version_info"][0])')"
if [[ "$major" != 20 ]]; then
  echo "Positioned valuation extension does not apply to Odoo ${major}"
  exit 0
fi
name="odoo-valuation-${RANDOM}-$$"
cleanup() {
  docker rm -f "${name}-tests" "${name}-db" >/dev/null 2>&1 || true
  docker network rm "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create --internal "$name" >/dev/null
docker run -d --name "${name}-db" --network "$name" \
  -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=synthetic-valuation \
  ghcr.io/baosystems/postgis:17-3.5 >/dev/null
for _ in {1..60}; do
  if docker exec "${name}-db" pg_isready -U odoo >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "${name}-db" pg_isready -U odoo >/dev/null
docker run --rm --name "${name}-tests" --network "$name" \
  -v "$fixture:/opt/valuation-tests:ro" \
  -e VALUATION_TEST_TAGS="$test_tags" \
  -e ODOO_DB_HOST="${name}-db" -e ODOO_DB_USER=odoo \
  -e ODOO_DB_PASSWORD=synthetic-valuation \
  -e ODOO_ADDONS_PATH=/opt/valuation-tests,/opt/launchplane/addons,/odoo/addons,/odoo/odoo/addons \
  --entrypoint /bin/bash "$image" -lc '
    set -euo pipefail
    odoo-bin -d valuation_test --init valuation_checkpoint_tests --without-demo \
      --test-tags "$VALUATION_TEST_TAGS" --stop-after-init --max-cron-threads=0 \
      --http-interface=127.0.0.1 --log-level=warn --log-handler=odoo.tests:INFO --log-handler=odoo.addons.valuation_checkpoint_tests:INFO 2>&1 | tee /tmp/valuation-tests.log
    grep -Eq "odoo[.]tests[.]stats: valuation_checkpoint_tests: [1-9][0-9]* tests" /tmp/valuation-tests.log
    grep -Eq "odoo[.]tests[.]result: 0 failed, 0 error[(]s[)] of [1-9][0-9]* tests" /tmp/valuation-tests.log
  '
