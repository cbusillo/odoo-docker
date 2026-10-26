"""Run only through Odoo shell against a newly created, isolated fixture database."""
import hashlib
import json
from pathlib import Path

from odoo.api import Environment


def create_gallery(env: Environment) -> str:
    contents = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><title>Gallery image {index}</title>'
        f'<rect width="2" height="2" fill="#{index:06x}"/></svg>'.encode()
        for index in range(400)
    ]
    attachments = env["ir.attachment"].create(
        [
            {"name": f"readiness-gallery-{index}.svg", "raw": content, "mimetype": "image/svg+xml", "public": True}
            for index, content in enumerate(contents)
        ]
    )
    media = {
        f"/web/content/{attachment.id}/{attachment.name}": hashlib.sha256(content).hexdigest()
        for attachment, content in zip(attachments, contents, strict=True)
    }
    Path("/fixture/media.json").write_text(json.dumps(media))
    return "".join(f'<img src="{path}" alt="Gallery fixture"/>' for path in media)


def setup(env: Environment) -> None:
    website = env["website"].browse(1)
    website.write({"domain": "http://fixture.invalid:8069"})
    env["website.assets"].with_context(website_id=website.id).make_scss_customization(
        "/website/static/src/scss/options/user_values.scss", {
            "font": "'SYSTEM_FONTS'", "headings-font": "'SYSTEM_FONTS'",
            "navbar-font": "'SYSTEM_FONTS'", "buttons-font": "'SYSTEM_FONTS'",
        },
    )
    gallery = create_gallery(env)
    for path, marker, key in [
        ("/", "Readiness fixture homepage", "fixture.home"),
        ("/readiness-fixture", "Readiness fixture public page", "fixture.public"),
    ]:
        view = env["ir.ui.view"].create({
            "name": key, "key": key, "type": "qweb",
            "arch": f'<t t-name="{key}"><t t-call="website.layout"><div id="wrap">'
                    f'<h1>{marker}</h1><img src="/web/image/website/1/logo" alt="Fixture logo"/>'
                    '<img src="/web/static/img/favicon.ico" alt="Fixture static media"/>'
                    f'{gallery}'
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

    parameters = env["ir.config_parameter"].sudo()
    uuid = parameters.search([("key", "=", "database.uuid")], limit=1).value
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
    parameters.create({"key": "launchplane.readiness.release", "value": json.dumps(release)})
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
