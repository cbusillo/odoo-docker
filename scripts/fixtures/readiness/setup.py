"""Run only through Odoo shell against a newly created, isolated fixture database."""
import json
from pathlib import Path

from odoo.api import Environment


def setup(env: Environment) -> None:
    website = env["website"].browse(1)
    website.write({"domain": "http://fixture.invalid:8069"})
    env["website.assets"].with_context(website_id=website.id).make_scss_customization(
        "/website/static/src/scss/options/user_values.scss", {
            "font": "'SYSTEM_FONTS'", "headings-font": "'SYSTEM_FONTS'",
            "navbar-font": "'SYSTEM_FONTS'", "buttons-font": "'SYSTEM_FONTS'",
        },
    )
    for path, marker, key in [
        ("/", "Readiness fixture homepage", "fixture.home"),
        ("/readiness-fixture", "Readiness fixture public page", "fixture.public"),
    ]:
        view = env["ir.ui.view"].create({
            "name": key, "key": key, "type": "qweb",
            "arch": f'<t t-name="{key}"><t t-call="website.layout"><div id="wrap">'
                    f'<h1>{marker}</h1><img src="/web/image/website/1/logo" alt="Fixture logo"/>'
                    '<img src="/web/static/img/favicon.ico" alt="Fixture static media"/>'
                    '</div></t></t>',
        })
        env["website.page"].create({
            "view_id": view.id, "url": path, "website_id": website.id, "is_published": True,
        })
        if path == "/":
            homepage = env["website.page"].search([
                ("url", "=", "/"), ("website_id", "=", website.id),
            ], limit=1).view_id
            assert homepage
            homepage.arch = view.arch

    uuid = env["ir.config_parameter"].sudo().get_param("database.uuid")
    identity = {
        "artifact_id": "isolated-fixture", "image_digest": "sha256:" + "a" * 64,
        "source_revision": "b" * 40, "slot": "candidate", "release_id": "fixture-release",
        "database_uuid": uuid,
    }
    # This fresh DB contains no business keys, and the network is Docker-internal.
    # A live producer must supply these attestations after its real committed fence.
    assert not env["ir.mail_server"].search_count([])
    assert not env["mail.mail"].search_count([])
    release = {
        "runtime_identity": identity, "update_id": "fixture-update", "updated_modules": ["website"],
        "status": "pass", "fences": {
            "credentials_committed": True, "mail_disabled": True, "integrations_disabled": True,
        },
    }
    env["ir.config_parameter"].sudo().set_param("launchplane.readiness.release", json.dumps(release))
    contract = {
        "schema_version": 1, "database": env.cr.dbname, "database_uuid": uuid,
        "host": "fixture.invalid:8069", "website_id": website.id,
        "runtime_identity": identity, "modules": ["base", "web", "website"], "release": release,
        "routes": [
            {"path": "/", "kind": "homepage", "contains": "Readiness fixture homepage"},
            {"path": "/web/login", "kind": "login", "contains": "oe_login_form"},
            {"path": "/readiness-fixture", "kind": "public", "contains": "Readiness fixture public page"},
        ],
    }
    env.cr.commit()
    Path("/fixture/contract.json").write_text(json.dumps(contract))



setup(globals()["env"])
