from __future__ import annotations

import json
import logging
import os
from ipaddress import ip_address

import psycopg2

from odoo.tools import config

from odoo import http
from odoo.http import request

from .readiness import ReadinessFailure, validate_contract, validate_observations


RUNTIME_IDENTITY_ENV_KEY = "LAUNCHPLANE_RUNTIME_IDENTITY_JSON"
_logger = logging.getLogger(__name__)


class LaunchplaneRuntimeHealthController(http.Controller):
    @http.route(
        "/launchplane/readiness",
        type="http",
        auth="none",
        methods=["POST"],
        save_session=False,
        csrf=False,
        readonly=True,
        max_content_length=65536,
    )
    def launchplane_readiness(self, **_params) -> http.Response:
        # Use the transport peer, never the address substituted by ProxyFix.
        environ = request.httprequest.environ
        original = environ.get("werkzeug.proxy_fix.orig", environ)
        peer = original.get("REMOTE_ADDR", "")
        try:
            local = ip_address(peer).is_loopback
        except ValueError:
            local = False
        forwarded = any(
            key == "HTTP_FORWARDED" or key.startswith("HTTP_X_FORWARDED_")
            for key in environ
        )
        if not local or forwarded:
            return self._readiness_response("private_probe_required", 403)

        try:
            payload = request.get_json_data()
            contract = validate_contract(payload["contract"])
            if request.httprequest.host != contract["host"]:
                raise ReadinessFailure("host_mismatch")
            database = contract["database"]
            if (
                request.db != database
                or request.httprequest.headers.get("X-Odoo-Database") != database
                or http.db_filter([database], host=contract["host"]) != [database]
                or request.httprequest.cookies
            ):
                raise ReadinessFailure("database_mismatch")
            registry = request.registry
            if not registry or not registry.ready or not registry.loaded:
                raise ReadinessFailure("registry_unavailable")
            if not set(contract["modules"]).issubset(registry._init_modules):
                raise ReadinessFailure("modules_not_loaded")
            if config["max_cron_threads"] != 0:
                raise ReadinessFailure("cron_not_fenced")
            identity = json.loads(os.environ.get(RUNTIME_IDENTITY_ENV_KEY, "null"))
            if identity != contract["runtime_identity"]:
                raise ReadinessFailure("identity_mismatch")

            # SQL only: no registry creation, ORM hooks, render, warming or writes.
            cr = request.env.cr
            cr.execute("SELECT key, value FROM ir_config_parameter WHERE key IN %s", [
                ("database.uuid", "launchplane.readiness.release", "base.partially_updated_database"),
            ])
            parameters = dict(cr.fetchall())
            if parameters.get("database.uuid") != contract["database_uuid"]:
                raise ReadinessFailure("database_identity_mismatch")
            if "base.partially_updated_database" in parameters:
                raise ReadinessFailure("update_pending")
            cr.execute("SELECT name, state FROM ir_module_module")
            states = dict(cr.fetchall())
            if any(state in {"to install", "to upgrade", "to remove"} for state in states.values()):
                raise ReadinessFailure("update_pending")
            if any(states.get(name) != "installed" for name in contract["modules"]):
                raise ReadinessFailure("modules_not_installed")
            # Use Odoo's Host-to-Website resolver, explicitly prohibiting fallback.
            # Website remains optional for liveness; readiness needs its native
            # strict Host resolver, without installing business modules here.
            website = request.env.get("website")
            if website is None:
                raise ReadinessFailure("website_unavailable")
            resolver = getattr(website.sudo(), "_get_current_website_id")
            if not callable(resolver):
                raise ReadinessFailure("website_unavailable")
            if resolver(contract["host"], fallback=False) != contract["website_id"]:
                raise ReadinessFailure("website_mismatch")
            release = json.loads(parameters.get("launchplane.readiness.release", "null"))
            if release != contract["release"]:
                raise ReadinessFailure("update_evidence_mismatch")
            validate_observations(contract, payload["observations"])
        except ReadinessFailure as exc:
            return self._readiness_response(exc.code, 503)
        except (ValueError, TypeError, KeyError, AttributeError, psycopg2.Error):
            # Database/configuration errors never expose inventories or raw values.
            _logger.warning("Readiness evidence is unavailable")
            return self._readiness_response("evidence_unavailable", 503)
        return self._readiness_response(None, 200)

    @staticmethod
    def _readiness_response(error: str | None, status: int) -> http.Response:
        body = {"status": "fail", "reason": error} if error else {"status": "pass"}
        return request.make_response(
            json.dumps(body, separators=(",", ":"), sort_keys=True),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-store")],
            status=status,
        )

    @http.route(
        "/launchplane/health",
        type="http",
        auth="none",
        methods=["GET"],
        save_session=False,
        csrf=False,
    )
    def launchplane_health(self) -> http.Response:
        body: dict[str, object] = {"status": "pass", "runtime_identity": None}
        raw_runtime_identity = os.environ.get(RUNTIME_IDENTITY_ENV_KEY, "").strip()
        status = 200

        if raw_runtime_identity:
            try:
                parsed_runtime_identity = json.loads(raw_runtime_identity)
            except json.JSONDecodeError:
                body = {"status": "fail", "runtime_identity_error": "malformed"}
                status = 500
            else:
                if isinstance(parsed_runtime_identity, dict):
                    body["runtime_identity"] = parsed_runtime_identity
                else:
                    body = {"status": "fail", "runtime_identity_error": "not_object"}
                    status = 500

        return request.make_response(
            json.dumps(body, separators=(",", ":"), sort_keys=True),
            headers=[
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-store"),
            ],
            status=status,
        )
