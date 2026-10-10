"""Exercise real Odoo HTTP/SQL pass/fail behavior from inside the isolated candidate."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import psycopg2

sys.path[:0] = ["/odoo", "/opt"]
from launchplane.addons.launchplane_runtime_health.controllers.readiness import fingerprint

root = Path("/fixture")
contract = json.loads((root / "contract.json").read_text())
command = ["/usr/local/bin/launchplane-readiness", "prepare", "--contract", str(root / "contract.json"),
           "--evidence", str(root / "browser.json")]
observations = json.loads(subprocess.check_output(command))
(root / "observations.json").write_text(json.dumps(observations))
opener = build_opener(ProxyHandler({}))
connection = psycopg2.connect(host=os.environ["ODOO_DB_HOST"], user=os.environ["ODOO_DB_USER"],
                            password=os.environ["ODOO_DB_PASSWORD"], dbname=contract["database"])


def snapshot() -> list:
    with connection.cursor() as cursor:
        cursor.execute("""SELECT 'attachment', id, write_date FROM ir_attachment
                          UNION ALL SELECT 'visitor', id, write_date FROM website_visitor
                          UNION ALL SELECT 'parameter', id, write_date FROM ir_config_parameter
                          UNION ALL SELECT 'module', id, write_date FROM ir_module_module
                          UNION ALL SELECT 'mail', id, write_date FROM mail_mail
                          ORDER BY 1, 2""")
        rows = cursor.fetchall()
    connection.commit()
    return rows


def probe(probe_contract: dict, probe_evidence: dict, *, host=None, database=None, extra_headers=None) -> tuple[int, dict]:
    headers = {"Host": host or probe_contract["host"], "X-Odoo-Database": database or probe_contract["database"],
               "Content-Type": "application/json", **(extra_headers or {})}
    req = Request("http://127.0.0.1:8069/launchplane/readiness", headers=headers,
                  data=json.dumps({"contract": probe_contract, "observations": probe_evidence}).encode())
    try:
        response = opener.open(req, timeout=10)
    except HTTPError as error:
        response = error
    probe_result = json.loads(response.read())
    assert "Set-Cookie" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    return response.status, probe_result


before = snapshot()
assert probe(contract, observations) == (200, {"status": "pass"})
subprocess.check_call(["/usr/local/bin/launchplane-readiness", "check", "--contract", str(root / "contract.json"),
                       "--evidence", str(root / "observations.json")])
cases = []
for field, value in [("database_uuid", "wrong"), ("website_id", 999), ("modules", ["base", "web", "website", "missing"])]:
    changed = copy.deepcopy(contract)
    changed[field] = value
    if field == "database_uuid":
        changed["runtime_identity"]["database_uuid"] = value
        changed["release"]["runtime_identity"]["database_uuid"] = value
    evidence = copy.deepcopy(observations)
    evidence["contract_sha256"] = fingerprint(changed)
    cases.append((field, changed, evidence, {}))
changed = copy.deepcopy(contract)
changed["runtime_identity"]["slot"] = "old-slot"
changed["release"]["runtime_identity"]["slot"] = "old-slot"
cases.append(("slot", changed, observations, {}))
for name, kwargs in [("wrong-host", {"host": "wrong.invalid"}),
                     ("wrong-db", {"database": "missing_database"}),
                     ("forwarded", {"extra_headers": {"X-Forwarded-For": "127.0.0.1"}})]:
    cases.append((name, contract, observations, kwargs))
for name, change in [
    ("stale-probe", lambda observed_value: observed_value.update(checked_at=observed_value["checked_at"] - 31)),
    ("failed-render", lambda observed_value: observed_value["routes"][0].update(status=500)),
    ("missing-asset", lambda observed_value: observed_value["assets"][0].update(status=404)),
    ("stale-browser", lambda observed_value: observed_value["browser"].update(checked_at=observed_value["browser"]["checked_at"] - 31)),
    ("failed-browser", lambda observed_value: observed_value["browser"].update(errors=["script failed"])),
]:
    changed = copy.deepcopy(observations)
    change(changed)
    cases.append((name, contract, changed, {}))
for name, expected, evidence, kwargs in cases:
    status, result = probe(expected, evidence, **kwargs)
    assert status in {403, 503} and result["status"] == "fail", (name, status, result)
    assert expected["database"] not in json.dumps(result), result
    print("readiness rejected:", name)
assert snapshot() == before, "Observational readiness changed database state"
connection.close()

# Actual failed renders/assets must prevent the preparation receipt, too.
broken = copy.deepcopy(contract)
broken["routes"][2]["path"] = "/missing-fixture-page"
(root / "broken.json").write_text(json.dumps(broken))
result = subprocess.run([*command[:3], str(root / "broken.json"), *command[4:]], capture_output=True)
assert result.returncode != 0
# Remove real referenced media in this disposable candidate, then verify preparation fails.
media = Path("/odoo/addons/web/static/img/favicon.ico")
content = media.read_bytes()
try:
    media.unlink()
    assert subprocess.run(command, capture_output=True).returncode != 0
finally:
    media.write_bytes(content)
print("preparation rejected real missing page and media")
print("readiness HTTP evidence passed")
