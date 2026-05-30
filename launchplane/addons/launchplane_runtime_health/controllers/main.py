import json
import os

from odoo import http
from odoo.http import request


RUNTIME_IDENTITY_ENV_KEY = "LAUNCHPLANE_RUNTIME_IDENTITY_JSON"


class LaunchplaneRuntimeHealthController(http.Controller):
    @http.route(
        "/launchplane/health",
        type="http",
        auth="none",
        methods=["GET"],
        save_session=False,
        csrf=False,
    )
    def launchplane_health(self):
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
