import os
from datetime import datetime

from odoo.api import Environment
from odoo.tools import sql

VALUATION_DATE = datetime(2024, 1, 1, 12, 0, 0, 123456)
DESCRIPTION = "Retained native cost before checkpoint schema upgrade"
UNIT_COST = 20
AUDIT_COST = 80 / 6
LEGACY_COSTS = ((80 / 6, None), (80 / 6, 80 / 6), (0, 80 / 6))
LEGACY_DESCRIPTION = "Retained positioned cost before precision upgrade"


def setup_legacy_checkpoints(env: Environment) -> None:
    company = env["res.company"].create({"name": "Legacy checkpoint upgrade company"})
    env = env["product.value"].with_company(company).env
    category = env["product.category"].create({"name": "Legacy checkpoint fixture", "property_cost_method": "average"})
    products = env["product.product"].create(
        [{"name": f"Legacy checkpoint {index}", "categ_id": category.id, "is_storable": True} for index in range(len(LEGACY_COSTS))]
    )
    native_positions = "after_move_id" in env["product.value"]._fields
    stock_location = env["stock.warehouse"].search([("company_id", "=", env.company.id)], limit=1).lot_stock_id
    assert stock_location
    if not sql.column_exists(env.cr, "product_value", "after_move_id"):
        env.cr.execute("ALTER TABLE product_value ADD COLUMN after_move_id integer")
    for index, (product, (cost, audit_cost)) in enumerate(zip(products, LEGACY_COSTS, strict=True)):
        move = env["stock.move"].create(
            {
                "product_id": product.id,
                "uom_id": product.uom_id.id,
                "product_uom_qty": 6,
                "location_id": env.ref("stock.stock_location_suppliers").id,
                "location_dest_id": stock_location.id,
            }
        )
        move._action_confirm(merge=False)
        move._action_assign()
        move.quantity = 6
        move.picked = True
        env["product.value"].create({"move_id": move.id, "value": 60})
        move._action_done()
        move.date = VALUATION_DATE
        move.move_line_ids.date = VALUATION_DATE
        values = {
            "product_id": product.id,
            "company_id": env.company.id,
            "value": cost,
            "date": VALUATION_DATE,
            "description": f"{LEGACY_DESCRIPTION} {index}",
        }
        if audit_cost is not None:
            values["new_cost"] = audit_cost
        if native_positions:
            values["after_move_id"] = move.id
        checkpoint = env["product.value"].create(values)
        env.flush_all()
        if not native_positions:
            env.cr.execute("UPDATE product_value SET after_move_id = %s WHERE id = %s", [move.id, checkpoint.id])
        env.cr.execute("SELECT value::text, new_cost::text FROM product_value WHERE id = %s", [checkpoint.id])
        print(f"legacy checkpoint {index}: {env.cr.fetchone()}")


def setup(env: Environment) -> None:
    category = env["product.category"].create({"name": "Upgrade valuation fixture", "property_cost_method": "average"})
    product = env["product.product"].create({"name": "Upgrade valuation fixture", "categ_id": category.id, "is_storable": True})
    env["product.value"].create(
        {
            "product_id": product.id,
            "company_id": env.company.id,
            "value": UNIT_COST,
            "new_cost": AUDIT_COST,
            "date": VALUATION_DATE,
            "description": DESCRIPTION,
        }
    )
    setup_legacy_checkpoints(env)
    env.cr.commit()


def verify(env: Environment) -> None:
    value = env["product.value"].search([("description", "=", DESCRIPTION)])
    assert len(value) == 1
    assert value.date == VALUATION_DATE
    assert value.value == UNIT_COST
    assert value.new_cost == AUDIT_COST
    assert not value.after_move_id
    env.invalidate_all()
    env.add_to_compute(value._fields["date"], value)
    value._recompute_recordset(["date"])
    env.flush_all()
    env.invalidate_all()
    assert value.date == VALUATION_DATE
    for index, (cost, _audit_cost) in enumerate(LEGACY_COSTS):
        checkpoint = env["product.value"].search([("description", "=", f"{LEGACY_DESCRIPTION} {index}")])
        assert len(checkpoint) == 1
        assert checkpoint.value == checkpoint.currency_id.round(cost)
        assert checkpoint.new_cost == checkpoint.value
        assert checkpoint.date == VALUATION_DATE == checkpoint.after_move_id.date
        product = checkpoint.product_id.with_company(checkpoint.company_id)._with_company_stock_context()._with_valuation_context()
        quantity = product.qty_available
        _costs, values = product._run_avco(at_date=VALUATION_DATE)
        assert quantity == checkpoint.after_move_id.quantity
        assert checkpoint.currency_id.is_zero(values[product.id] - quantity * checkpoint.value)
        print(f"upgraded legacy checkpoint {index}: retained value {checkpoint.value}, stock value {values[product.id]}")
    print("native stock_account upgrade preserves unanchored cost and exact date")


if os.environ["VALUATION_UPGRADE_PHASE"] == "setup":
    setup(globals()["env"])
else:
    verify(globals()["env"])
