env = globals()["env"]
module = env["ir.module.module"].search([("name", "=", "product_sequence")]).ensure_one()
assert module.state == "uninstalled", module.state
fixture_ids = []
for kind in ("consu", "service"):
    for active in (True, False):
        identifiers = (
            (False, False),
            ("/", f"barcode-{kind}-{active}"),
            (f"existing-{kind}-{active}", "/" if kind == "consu" and active else False),
        )
        for code, barcode in identifiers:
            product = env["product.product"].create({"name": f"Fixture {kind} {active} {code}", "type": kind, "default_code": code})
            product.barcode = barcode
            if not active:
                product.product_tmpl_id.action_archive()
            fixture_ids.append(product.id)

env.flush_all()


def stored_identifiers() -> list[tuple]:
    env.cr.execute(
        """
        SELECT p.id, p.active, p.default_code, p.barcode, t.id, t.active, t.default_code, t.barcode
          FROM product_product p JOIN product_template t ON t.id = p.product_tmpl_id
         WHERE p.id IN %s ORDER BY p.id
        """,
        [tuple(fixture_ids)],
    )
    return env.cr.fetchall()


before = stored_identifiers()
assert len(before) == len(fixture_ids)
assert {row[2] for row in before} >= {None, "/"}
module.button_immediate_install()
after = stored_identifiers()
assert after == before, (before, after)
assert module.state == "installed", module.state
