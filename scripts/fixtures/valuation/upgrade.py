import os
from datetime import datetime

from odoo.api import Environment

VALUATION_DATE = datetime(2024, 1, 1, 12, 0, 0, 123456)
DESCRIPTION = "Retained native cost before checkpoint schema upgrade"
UNIT_COST = 20


def setup(env: Environment) -> None:
    category = env["product.category"].create({"name": "Upgrade valuation fixture", "property_cost_method": "average"})
    product = env["product.product"].create({"name": "Upgrade valuation fixture", "categ_id": category.id, "is_storable": True})
    env["product.value"].create(
        {
            "product_id": product.id,
            "company_id": env.company.id,
            "value": UNIT_COST,
            "date": VALUATION_DATE,
            "description": DESCRIPTION,
        }
    )
    env.cr.commit()


def verify(env: Environment) -> None:
    value = env["product.value"].search([("description", "=", DESCRIPTION)])
    assert len(value) == 1
    assert value.date == VALUATION_DATE
    assert value.value == UNIT_COST
    assert not value.after_move_id
    env.invalidate_all()
    env.add_to_compute(value._fields["date"], value)
    value._recompute_recordset(["date"])
    env.flush_all()
    env.invalidate_all()
    assert value.date == VALUATION_DATE
    print("native stock_account upgrade preserves unanchored cost and exact date")


if os.environ["VALUATION_UPGRADE_PHASE"] == "setup":
    setup(globals()["env"])
else:
    verify(globals()["env"])
