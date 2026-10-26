"""Faults in an actual loaded Odoo registry, invoked via the isolated Odoo shell."""
import json
from pathlib import Path
from unittest.mock import patch

from odoo import http
from odoo.addons.launchplane_runtime_health.controllers import main as controller_module
from odoo.api import Environment
from odoo.tools import config
from werkzeug.wrappers import Request as WerkzeugRequest


def check_registry(env: Environment) -> None:
    root = Path("/fixture")
    contract = json.loads((root / "contract.json").read_text())
    observations = json.loads((root / "observations.json").read_text())
    req = http.Request(WerkzeugRequest.from_values(
        method="POST", data=json.dumps({"contract": contract, "observations": observations}),
        headers={"Host": contract["host"], "X-Odoo-Database": contract["database"], "Content-Type": "application/json"},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    ))
    req.db, req.env, req.registry = env.cr.dbname, env, env.registry
    controller = controller_module.LaunchplaneRuntimeHealthController()
    with patch.dict(vars(controller_module), {"request": req}):
        previous_filter = config["dbfilter"]
        try:
            config["dbfilter"] = "^unmatched_fixture_database$"
            response = controller.launchplane_readiness()
            assert response.status_code == 503
            assert json.loads(response.get_data())["reason"] == "database_mismatch"
        finally:
            config["dbfilter"] = previous_filter
        print("readiness rejected a database outside the native filter")
        website = env["website"].browse(contract["website_id"])
        assert controller_module._website_id_for_host(website, contract["host"].split(":")[0]) == website.id
        previous_domain = website.domain
        try:
            website.domain = False
            response = controller.launchplane_readiness()
            assert response.status_code == 503
            assert json.loads(response.get_data())["reason"] == "website_mismatch"
        finally:
            website.domain = previous_domain
        print("readiness rejected a Website without a configured domain")
        # Positive admission is tested over HTTP with fresh browser evidence.
        # These faults occur before observation validation; assert their exact
        # reasons so elapsed time cannot masquerade as the planted fault.
        for field in ("ready", "loaded"):
            previous = getattr(env.registry, field)
            try:
                setattr(env.registry, field, False)
                response = controller.launchplane_readiness()
                assert response.status_code == 503
                assert json.loads(response.get_data())["reason"] == "registry_unavailable"
            finally:
                setattr(env.registry, field, previous)
            print("readiness rejected real registry fault:", field)
        previous_modules = env.registry._init_modules
        try:
            env.registry._init_modules = previous_modules - {"website"}
            response = controller.launchplane_readiness()
            assert json.loads(response.get_data())["reason"] == "modules_not_loaded"
        finally:
            env.registry._init_modules = previous_modules
        print("readiness rejected real unloaded module")


check_registry(globals()["env"])
