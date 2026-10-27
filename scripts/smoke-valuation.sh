#!/usr/bin/env bash
set -euo pipefail
image="${1:?Usage: scripts/smoke-valuation.sh <runtime-image>}"
test_tags="${2:-/valuation_checkpoint_tests,/stock_account,/stock:TestVirtualAvailable,/stock:TestProductPostInstall,/stock:TestLotSerial,/stock:TestStockQuant.test_to_date_consignment,/stock:TestReportStockQuantity}"
patch_file="$(cd "$(dirname "$0")/../patches" && pwd)/20.0-valuation-checkpoints.patch"
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
odoo_run=(docker run --rm --name "${name}-tests" --network "$name"
  -v "$fixture:/opt/valuation-tests:ro" -v "$patch_file:/opt/native-valuation.patch:ro"
  -e VALUATION_TEST_TAGS="$test_tags"
  -e ODOO_DB_HOST="${name}-db" -e ODOO_DB_USER=odoo
  -e PGHOST="${name}-db" -e PGUSER=odoo -e PGPORT=5432
  -e ODOO_DB_PASSWORD=synthetic-valuation
  -e ODOO_ADDONS_PATH=/opt/valuation-tests,/opt/launchplane/addons,/odoo/addons,/odoo/odoo/addons
  --entrypoint /bin/bash "$image" -lc)
"${odoo_run[@]}" '
  set -euo pipefail
  git -C /odoo apply --reverse /opt/native-valuation.patch
  odoo-bin -d valuation_test --init stock_account --without-demo --stop-after-init \
    --max-cron-threads=0 --http-interface=127.0.0.1 --log-level=warn
  VALUATION_UPGRADE_PHASE=setup odoo-bin shell -d valuation_test --no-http \
    --max-cron-threads=0 --log-level=warn < /opt/valuation-tests/upgrade.py
'
"${odoo_run[@]}" '
  set -euo pipefail
  odoo-bin -d valuation_test --update stock,stock_account --init valuation_checkpoint_tests --without-demo \
    --test-tags "$VALUATION_TEST_TAGS" --stop-after-init --max-cron-threads=0 \
    --http-interface=127.0.0.1 --log-level=warn --log-handler=odoo.tests:INFO \
    --log-handler=odoo.addons.valuation_checkpoint_tests:INFO 2>&1 | tee /tmp/valuation-tests.log
  grep -Eq "odoo[.]tests[.]stats: valuation_checkpoint_tests: [1-9][0-9]* tests" /tmp/valuation-tests.log
  if [[ "$VALUATION_TEST_TAGS" == *"/stock_account"* ]]; then
    grep -Eq "odoo[.]tests[.]stats: stock_account: [1-9][0-9]* tests" /tmp/valuation-tests.log
  fi
  if [[ "$VALUATION_TEST_TAGS" == *"/stock:"* ]]; then
    grep -Eq "odoo[.]tests[.]stats: stock: [1-9][0-9]* tests" /tmp/valuation-tests.log
  fi
  VALUATION_UPGRADE_PHASE=verify odoo-bin shell -d valuation_test --no-http \
    --max-cron-threads=0 --log-level=warn < /opt/valuation-tests/upgrade.py
'
