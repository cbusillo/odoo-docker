#!/usr/bin/env bash
set -euo pipefail
image="${1:?Usage: scripts/smoke-valuation.sh <runtime-image>}"
test_tags="${2:-/valuation_checkpoint_tests,/stock_account,/stock:TestVirtualAvailable,/stock:TestProductPostInstall,/stock:TestLotSerial,/stock:TestStockQuant.test_to_date_consignment,/stock:TestReportStockQuantity}"
access_safety=true
if [[ -n "${2:-}" ]]; then access_safety=false; fi
previous_image="${3:-$image}"
reverse_patch=true
if [[ -n "${3:-}" ]]; then reverse_patch=false; fi
patch_file="$(cd "$(dirname "$0")/../patches" && pwd)/20.0-valuation-checkpoints.patch"
access_patch_file="$(cd "$(dirname "$0")/../patches" && pwd)/20.0-zero-access-data.patch"
profile_patch_file="$(cd "$(dirname "$0")/../patches" && pwd)/20.0-self-profile-access.patch"
fixture="$(cd "$(dirname "$0")/fixtures/valuation" && pwd)"
access_fixture="$(cd "$(dirname "$0")/fixtures/access" && pwd)"
name="odoo-valuation-${RANDOM}-$$"
reader_id=""
tests_id=""
db_id=""
network_id=""
cleanup() {
  local smoke_status=$?
  if (( smoke_status != 0 )); then
    if [[ -n "$db_id" ]]; then docker logs --tail 80 "$db_id" >&2 || true; fi
  fi
  if [[ -n "$reader_id" ]]; then docker rm -fv "$reader_id" >/dev/null 2>&1 || true; fi
  if [[ -n "$tests_id" ]]; then docker rm -fv "$tests_id" >/dev/null 2>&1 || true; fi
  if [[ -n "$db_id" ]]; then docker rm -fv "$db_id" >/dev/null 2>&1 || true; fi
  if [[ -n "$network_id" ]]; then docker network rm "$network_id" >/dev/null 2>&1 || true; fi
  return "$smoke_status"
}
trap cleanup EXIT
reader_id="$(docker create --name "${name}-version" --network none --entrypoint /venv/bin/python "$image" -c 'import runpy; print(runpy.run_path("/odoo/odoo/release.py")["version_info"][0])')"
docker inspect --format 'Valuation version reader: {{.Id}} image: {{.Image}} mounts: {{json .Mounts}}' "$reader_id"
major="$(docker start --attach "$reader_id")"
docker rm -fv "$reader_id" >/dev/null
reader_id=""
if [[ "$major" != 20 ]]; then
  echo "Positioned valuation extension does not apply to Odoo ${major}"
  exit 0
fi
network_id="$(docker network create --internal "$name")"
echo "Valuation network: $network_id"
db_id="$(docker create --name "${name}-db" --network "$name" \
  -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=synthetic-valuation \
  ghcr.io/baosystems/postgis:17-3.5)"
docker inspect --format 'Valuation database: {{.Id}} mounts: {{json .Mounts}}' "$db_id"
docker start "$db_id" >/dev/null
for _ in {1..60}; do
  if docker exec "${name}-db" pg_isready -h 127.0.0.1 -U odoo >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "${name}-db" pg_isready -h 127.0.0.1 -U odoo >/dev/null
odoo_run=(docker create --name "${name}-tests" --network "$name"
  -e VALUATION_TEST_TAGS="$test_tags"
  -e VALUATION_REVERSE_PATCH="$reverse_patch"
  -e NATIVE_ACCESS_SAFETY="$access_safety"
  -e NATIVE_ACCESS_SOURCE=/opt/access-tests/zero_access_19.csv
  -e ODOO_DB_HOST="${name}-db" -e ODOO_DB_USER=odoo
  -e PGHOST="${name}-db" -e PGUSER=odoo -e PGPORT=5432
  -e ODOO_DB_PASSWORD=synthetic-valuation
  -e 'ODOO_ADDONS_PATH=/opt/valuation-tests,/opt/access-tests,/opt/launchplane/addons,/odoo/addons,/odoo/odoo/addons'
  --entrypoint /bin/bash)
run_fixture() {
  local source_proof='
    set -euo pipefail
    record_sources() {
    sha256sum /opt/native-valuation.patch /opt/native-zero-access-data.patch /odoo/odoo/orm/fields_numeric.py \
      /odoo/addons/stock_account/models/product.py /odoo/addons/stock_account/models/product_value.py \
      /odoo/addons/stock_account/models/stock_move.py /odoo/addons/stock_account/__manifest__.py \
      /odoo/addons/stock/models/product.py /odoo/addons/stock/models/stock_lot.py \
      /odoo/addons/stock_account/report/stock_avco_audit_report.py
    find /opt/valuation-tests -type f -name "*.py" -exec sha256sum {} +
    find /odoo/addons/stock_account -path "*/migrations/*" -type f -name "*.py" -exec sha256sum {} +
    sha256sum /opt/native-profile-access.patch /odoo/odoo/orm/models.py \
      /odoo/odoo/addons/base/models/ir_access.py /odoo/odoo/addons/base/models/res_users.py \
      /odoo/odoo/addons/base/security/base_groups.xml /odoo/odoo/addons/base/security/ir.access.csv \
      /odoo/addons/mail/models/res_users.py /odoo/addons/sale_stock/models/res_users.py \
      /odoo/addons/auth_passkey/models/res_users.py /odoo/addons/auth_totp/models/res_users.py \
      /odoo/addons/mail_bot/models/res_users.py
    find /opt/access-tests -type f -name "*.py" -exec sha256sum {} +
    sha256sum /opt/access-tests/zero_access_19.csv
    }
  '
  tests_id="$("${odoo_run[@]}" "$1" -lc "$source_proof"$'\n'"$2")"
  docker cp "$fixture" "$tests_id:/opt/valuation-tests"
  docker cp "$patch_file" "$tests_id:/opt/native-valuation.patch"
  docker cp "$access_patch_file" "$tests_id:/opt/native-zero-access-data.patch"
  docker cp "$profile_patch_file" "$tests_id:/opt/native-profile-access.patch"
  docker cp "$access_fixture" "$tests_id:/opt/access-tests"
  docker inspect --format 'Valuation test: {{.Id}} image: {{.Image}} mounts: {{json .Mounts}}' "$tests_id"
  docker start --attach "$tests_id"
  docker rm -fv "$tests_id" >/dev/null
  tests_id=""
}
# shellcheck disable=SC2016
run_fixture "$previous_image" '
  set -euo pipefail
  if [[ "$VALUATION_REVERSE_PATCH" == true ]]; then
    git -C /odoo apply --reverse /opt/native-valuation.patch
    git -C /odoo apply --reverse /opt/native-zero-access-data.patch
  fi
  record_sources
  access_modules="$(NATIVE_ACCESS_PHASE=owners /venv/bin/python /opt/access-tests/verify.py)"
  odoo-bin -d valuation_test --init "stock_account,test_base,valuation_checkpoint_tests,profile_access_tests,$access_modules" --without-demo --stop-after-init \
    --max-cron-threads=0 --http-interface=127.0.0.1 --log-level=warn
  VALUATION_UPGRADE_PHASE=setup odoo-bin shell -d valuation_test --no-http \
    --max-cron-threads=0 --log-level=warn < /opt/valuation-tests/upgrade.py
  NATIVE_ACCESS_PHASE=before odoo-bin shell -d valuation_test --no-http \
    --max-cron-threads=0 --log-level=warn < /opt/access-tests/verify.py
'
# shellcheck disable=SC2016
run_fixture "$image" '
  set -euo pipefail
  git -C /odoo apply --reverse --check /opt/native-valuation.patch
  git -C /odoo apply --reverse --check /opt/native-zero-access-data.patch
  git -C /odoo apply --reverse --check /opt/native-profile-access.patch
  record_sources
  access_test_modules="valuation_checkpoint_tests,test_base,profile_access_tests"
  if [[ "$NATIVE_ACCESS_SAFETY" == true ]]; then
    access_tags="$(/venv/bin/python - <<"PY"
import ast
from pathlib import Path
root = Path("/odoo/odoo/addons/test_base/tests/test_orm")
files = ("test_acl.py", "test_access.py", "test_access_feedback.py", "test_access_monetary_related.py", "test_check_access.py", "test_ir_rules.py")
classes = {node.name for name in files for node in ast.parse((root / name).read_text()).body if isinstance(node, ast.ClassDef)}
print(",".join("/test_base:" + name for name in sorted(classes)))
PY
)"
    VALUATION_TEST_TAGS="$VALUATION_TEST_TAGS,$access_tags,/profile_access_tests"
  fi
  access_modules="$(NATIVE_ACCESS_PHASE=owners /venv/bin/python /opt/access-tests/verify.py)"
  odoo-bin -d valuation_test --update "base,stock,stock_account,$access_modules" --init "$access_test_modules" --without-demo \
    --test-tags "$VALUATION_TEST_TAGS" --stop-after-init --max-cron-threads=0 \
    --http-interface=127.0.0.1 --log-level=warn --log-handler=odoo.tests:INFO \
    --log-handler=odoo.addons.valuation_checkpoint_tests:INFO \
    --log-handler=odoo.addons.profile_access_tests:INFO 2>&1 | tee /tmp/valuation-tests.log
  grep -Eq "odoo[.]tests[.]stats: valuation_checkpoint_tests: [1-9][0-9]* tests" /tmp/valuation-tests.log
  if [[ "$VALUATION_TEST_TAGS" == *"/stock_account"* ]]; then
    grep -Eq "odoo[.]tests[.]stats: stock_account: [1-9][0-9]* tests" /tmp/valuation-tests.log
  fi
  if [[ "$VALUATION_TEST_TAGS" == *"/stock:"* ]]; then
    grep -Eq "odoo[.]tests[.]stats: stock: [1-9][0-9]* tests" /tmp/valuation-tests.log
  fi
  if [[ "$NATIVE_ACCESS_SAFETY" == true ]]; then
    grep -Eq "odoo[.]tests[.]stats: test_base: [1-9][0-9]* tests" /tmp/valuation-tests.log
    grep -Eq "odoo[.]tests[.]stats: profile_access_tests: [1-9][0-9]* tests" /tmp/valuation-tests.log
  fi
  VALUATION_UPGRADE_PHASE=verify odoo-bin shell -d valuation_test --no-http \
    --max-cron-threads=0 --log-level=warn < /opt/valuation-tests/upgrade.py
  NATIVE_ACCESS_PHASE=capture odoo-bin shell -d valuation_test --no-http \
    --max-cron-threads=0 --log-level=warn < /opt/access-tests/verify.py
  odoo-bin -d valuation_test --update "$access_modules" --without-demo --stop-after-init \
    --max-cron-threads=0 --http-interface=127.0.0.1 --log-level=warn
  NATIVE_ACCESS_PHASE=verify odoo-bin shell -d valuation_test --no-http \
    --max-cron-threads=0 --log-level=warn < /opt/access-tests/verify.py
'
