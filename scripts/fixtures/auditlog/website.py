env = globals()["env"]
site = env["website"].create({"name": "Layout fixture"})
before = "<t><main>Original layout</main></t>"
after = "<t><main>Updated layout</main></t>"
view = env["ir.ui.view"].create(
    {"name": "Fixture layout", "key": "fixture.layout", "type": "qweb", "website_id": site.id, "arch_db": before}
)
assets = env["website.assets"].with_context(website_id=site.id)
stylesheet = "/website/static/src/scss/options/user_values.scss"
bundle = "web.assets_frontend"
assets.make_scss_customization(stylesheet, {"font-size-base": "1rem"})
rule = env["auditlog.rule"].create(
    {
        "name": "Fixture view writes",
        "model_id": env["ir.model"]._get(view._name).id,
        "log_type": "full",
        "log_create": False,
        "log_unlink": False,
        "log_export_data": False,
    }
)
rule.set_to_confirmed()

domain = "https://layout.example.invalid"
site.domain = domain
view.with_context(active_test=False).arch_db = after
assets.reset_asset(stylesheet, bundle)
assets.make_scss_customization(stylesheet, {"font-size-base": "1.1rem"})
assert site.domain == domain, site.domain

env.cr.commit()
with env.registry.cursor() as cursor:
    later = env(cr=cursor)
    cursor.execute("SELECT domain FROM website WHERE id = %s", [site.id])
    assert cursor.fetchone() == (domain,)
    assert later["website"].browse(site.id).domain == domain
    log = (
        later["auditlog.log"]
        .search([("model_model", "=", view._name), ("res_id", "=", view.id), ("method", "=", "write")])
        .ensure_one()
    )
    line = log.line_ids.filtered(lambda item: item.field_name == "arch_db").ensure_one()
    assert log.user_id == later.user
    assert (line.old_value_text, line.new_value_text) == (before, after)
