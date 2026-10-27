"""Exercise real Odoo HTTP/SQL pass/fail behavior from inside the isolated candidate."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import psycopg2

sys.path[:0] = ["/odoo", "/opt"]
from launchplane.addons.launchplane_runtime_health.controllers.readiness import fingerprint

root = Path("/fixture")
contract = json.loads((root / "contract.json").read_text())
command = [
    "/usr/local/bin/launchplane-readiness",
    "prepare",
    "--contract",
    str(root / "contract.json"),
    "--evidence",
    str(root / "browser.json"),
]
observations = json.loads((root / "observations.json").read_text())
expected_media = json.loads((root / "media.json").read_text())
observed_media = {asset["path"]: asset["sha256"] for asset in observations["assets"]}
assert all(observed_media.get(path) == digest for path, digest in expected_media.items()), "Gallery asset evidence was lost"
print(
    "complete readiness receipt:",
    len(json.dumps({"contract": contract, "observations": observations}, separators=(",", ":")).encode()),
    "bytes;",
    len(observations["assets"]),
    "assets",
)
(root / "observations.json").write_text(json.dumps(observations))
opener = build_opener(ProxyHandler({}))
connection = psycopg2.connect(
    host=os.environ["ODOO_DB_HOST"],
    user=os.environ["ODOO_DB_USER"],
    password=os.environ["ODOO_DB_PASSWORD"],
    dbname=contract["database"],
)


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


def probe(
    probe_contract: dict,
    probe_evidence: dict,
    *,
    host: str | None = None,
    database: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    headers = {
        "Host": host or probe_contract["host"],
        "X-Odoo-Database": database or probe_contract["database"],
        "Content-Type": "application/json",
        **(extra_headers or {}),
    }
    req = Request(
        "http://127.0.0.1:8069/launchplane/readiness",
        headers=headers,
        data=json.dumps({"contract": probe_contract, "observations": probe_evidence}, separators=(",", ":")).encode(),
    )
    try:
        response = opener.open(req, timeout=10)
    except HTTPError as error:
        response = error
    assert response.headers.get_content_type() == "application/json", f"Readiness returned HTTP {response.status}"
    probe_result = json.loads(response.read())
    assert "Set-Cookie" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    return response.status, probe_result


before = snapshot()
assert probe(contract, observations) == (200, {"status": "pass"})
unknown_host_contract = copy.deepcopy(contract)
unknown_host_contract["host"] = "unconfigured.invalid:8069"
unknown_host_evidence = copy.deepcopy(observations)
unknown_host_evidence["contract_sha256"] = fingerprint(unknown_host_contract)
unknown_host_evidence["browser"]["contract_sha256"] = fingerprint(unknown_host_contract)
assert probe(unknown_host_contract, unknown_host_evidence) == (503, {"status": "fail", "reason": "website_mismatch"})
print("readiness rejected unconfigured Host without default-site fallback")
subprocess.check_call(
    [
        "/usr/local/bin/launchplane-readiness",
        "check",
        "--contract",
        str(root / "contract.json"),
        "--evidence",
        str(root / "observations.json"),
    ]
)
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
for name, kwargs in [
    ("wrong-host", {"host": "wrong.invalid"}),
    ("wrong-db", {"database": "missing_database"}),
    ("forwarded", {"extra_headers": {"X-Forwarded-For": "127.0.0.1"}}),
]:
    cases.append((name, contract, observations, kwargs))
for name, change in [
    ("stale-probe", lambda observed_value: observed_value.update(checked_at=observed_value["checked_at"] - 31)),
    ("failed-render", lambda observed_value: observed_value["routes"][0].update(status=500)),
    ("missing-asset", lambda observed_value: observed_value["assets"][0].update(status=404)),
    (
        "stale-browser",
        lambda observed_value: observed_value["browser"].update(checked_at=observed_value["browser"]["checked_at"] - 31),
    ),
    ("failed-browser", lambda observed_value: observed_value["browser"].update(errors=["script failed"])),
]:
    changed = copy.deepcopy(observations)
    change(changed)
    cases.append((name, contract, changed, {}))
for name, expected, evidence, kwargs in cases:
    status, result = probe(expected, evidence, **kwargs)
    assert status in {403, 503} and result["status"] == "fail", (name, status, result)
    assert expected["database"] not in json.dumps(result), result
    if name == "stale-probe":
        rejected_evidence = root / "rejected-observations.json"
        rejected_evidence.write_text(json.dumps(evidence))
        rejected = subprocess.run(
            [
                "/usr/local/bin/launchplane-readiness",
                "check",
                "--contract",
                str(root / "contract.json"),
                "--evidence",
                str(rejected_evidence),
            ],
            capture_output=True,
            text=True,
        )
        assert rejected.returncode != 0
        assert result["reason"] in rejected.stderr, rejected.stderr
        assert expected["database"] not in rejected.stderr, rejected.stderr
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
