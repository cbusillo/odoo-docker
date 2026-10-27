from datetime import datetime, timedelta

from freezegun import freeze_time
from odoo import Command
from odoo.addons.stock_account.tests.common import TestStockValuationCommon
from odoo.exceptions import ValidationError


class TestValuationCheckpoint(TestStockValuationCommon):
    _test_user_groups = (
        "stock.group_stock_manager",
        "product.group_product_manager",
        "account.group_account_invoice",
        "account.group_account_readonly",
    )

    def _receipt(self, at: datetime, cost: float, **kwargs: object) -> "odoo.model.stock_move":
        with freeze_time(at):
            move = self._make_in_move(self.product_avco, 1, unit_cost=cost, **kwargs)
            move.date = at
            move.move_line_ids.date = at
            return move

    def _shipment(self, at: datetime) -> "odoo.model.stock_move":
        with freeze_time(at):
            move = self._make_out_move(self.product_avco, 1)
            move.date = at
            move.move_line_ids.date = at
            return move

    def _opening_stock(self) -> datetime:
        start = datetime(2024, 1, 2, 12)
        for index, cost in enumerate((10, 40, 40)):
            self._receipt(start + timedelta(seconds=index), cost)
        return start + timedelta(seconds=10)

    def _checkpoint(self, anchor: "odoo.model.stock_move", cost: float = 40) -> "odoo.model.product_value":
        return self.env["product.value"].create({"after_move_id": anchor.id, "value": cost})

    def _assert_cold_value(self, at: datetime, quantity: float, value: float) -> None:
        self.env.invalidate_all()
        product = self.product_avco._with_valuation_context().with_context(to_date=at)
        self.assertAlmostEqual(product.qty_available, quantity)
        self.assertAlmostEqual(product.total_value, value)

    def test_checkpoint_between_tied_outgoing_moves_survives_revaluation(self) -> None:
        at = self._opening_stock()
        cancellation = self._shipment(at)
        checkpoint = self._checkpoint(cancellation)
        shipment = self._shipment(at)
        self._receipt(at + timedelta(seconds=1), 10)
        self.assertEqual((cancellation.value, shipment.value), (-30, -40))
        self._assert_cold_value(at, 1, 40)
        self._assert_cold_value(at + timedelta(seconds=1), 2, 50)
        self.assertFalse(self.product_avco._correct_inventory_valuation(datetime.min))
        self.env.invalidate_all()
        self.assertEqual((cancellation.value, shipment.value), (-30, -40))
        self.assertEqual(checkpoint.date, cancellation.date)
        self.assertEqual(shipment.date, cancellation.date)
        self.assertEqual(shipment.move_line_ids.date, shipment.date)
        self._assert_cold_value(at + timedelta(seconds=1), 2, 50)

    def test_incoming_tie_and_adjacent_microseconds(self) -> None:
        original_product = self.product_avco
        for gap in (timedelta(), timedelta(microseconds=1)):
            with self.subTest(gap=gap), self.cr.savepoint():
                self.product_avco = original_product.copy()
                at = self._opening_stock().replace(microsecond=999999)
                cancellation = self._shipment(at)
                self._checkpoint(cancellation)
                receipt = self._receipt(at + gap, 10)
                shipment = self._shipment(at + timedelta(seconds=1))
                self.assertEqual((cancellation.value, shipment.value), (-30, -30))
                self._assert_cold_value(at + gap, 3, 90)
                self._assert_cold_value(at + timedelta(seconds=1), 2, 60)
                self.assertFalse(self.product_avco._correct_inventory_valuation(at))
                self.env.invalidate_all()
                self.assertEqual(shipment.value, -30)
                self.assertEqual(self.product_avco.standard_price, 30)
                self.assertEqual(self.product_avco.total_value, 60)
                self.assertEqual(receipt.date, at + gap)
                self.assertEqual(receipt.move_line_ids.date, at + gap)

    def test_fractional_history_excludes_later_move_and_checkpoint(self) -> None:
        at = self._opening_stock()
        cancellation = self._shipment(at + timedelta(microseconds=200))
        self._checkpoint(cancellation)
        self._receipt(at + timedelta(microseconds=700), 10)
        self._assert_cold_value(at + timedelta(microseconds=100), 3, 90)
        self._assert_cold_value(at + timedelta(microseconds=400), 2, 80)
        self._assert_cold_value(at + timedelta(microseconds=900), 3, 90)

    def test_anchor_date_and_checkpoint_edit_delete_replay(self) -> None:
        at = self._opening_stock()
        cancellation = self._shipment(at)
        checkpoint = self._checkpoint(cancellation)
        shipment = self._shipment(at)
        self.assertEqual(shipment.value, -40)
        checkpoint.value = 50
        self.assertEqual((cancellation.value, shipment.value), (-30, -50))
        cancellation.date = at + timedelta(seconds=2)
        cancellation.move_line_ids.date = cancellation.date
        self.assertEqual(checkpoint.date, cancellation.date)
        self.assertEqual(shipment.value, -30)
        checkpoint.unlink()
        self.assertEqual(self.product_avco.standard_price, 30)
        self._assert_cold_value(at + timedelta(seconds=3), 1, 30)

    def test_scoped_opening_quantity_ignores_consignment(self) -> None:
        at = self._opening_stock()
        owner = self.env["res.partner"].create({"name": "Consignment owner"})
        self._receipt(at - timedelta(seconds=1), 999, owner_id=owner.id)
        cancellation = self._shipment(at)
        self._checkpoint(cancellation)
        shipment = self._shipment(at)
        self.assertEqual(shipment.value, -40)
        self._assert_cold_value(at, 1, 40)

    def test_company_and_lot_positions_remain_independent(self) -> None:
        at = self._opening_stock()
        company = self.env["res.company"].sudo().create({"name": "Other checkpoint company"})
        self.env.user.sudo().write({"company_ids": [Command.link(company.id)]})
        self.product_avco.categ_id.with_company(company).property_cost_method = "average"
        with freeze_time(at - timedelta(seconds=1)):
            other_move = self._make_in_move(self.product_avco, 50, unit_cost=100, company=company)
        cancellation = self._shipment(at)
        self._checkpoint(cancellation)
        self.assertEqual(other_move.value, 5000)
        self._assert_cold_value(at, 2, 80)
        self.env.invalidate_all()
        self.assertEqual(self.product_avco.with_company(company).with_context(allowed_company_ids=company.ids).total_value, 5000)

        product = self.product_avco.copy({"tracking": "lot", "lot_valuated": True})
        lots = self.env["stock.lot"].create(
            [
                {"name": "First checkpoint lot", "product_id": product.id},
                {"name": "Second checkpoint lot", "product_id": product.id},
            ]
        )
        with freeze_time(at):
            first = self._make_in_move(product, 2, unit_cost=10, lot_ids=lots[:1])
            second = self._make_in_move(product, 2, unit_cost=100, lot_ids=lots[1:])
        lot_checkpoint = self.env["product.value"].create({"after_move_id": first.id, "lot_id": lots[0].id, "value": 40})
        with freeze_time(at + timedelta(seconds=1)):
            shipment = self._make_out_move(product, 1, lot_ids=lots[1:])
        self.assertEqual(shipment.value, -100)
        self.assertFalse(product._correct_inventory_valuation(datetime.min))
        self.env.invalidate_all()
        self.assertEqual(shipment.value, -100)
        self.assertEqual(second.value, 200)
        self.assertEqual(lots[0].total_value, 80)
        self.assertEqual(lots[1].total_value, 100)
        lot_checkpoint.lot_id = lots[1]
        self.assertEqual(lots[0].total_value, 20)
        self.assertEqual(shipment.value, -100)
        lot_checkpoint.after_move_id = second
        self.assertEqual(shipment.value, -40)
        with freeze_time(at + timedelta(seconds=1, microseconds=200)):
            next_shipment = self._make_out_move(product, 1, lot_ids=lots[1:])
        next_shipment.date = at + timedelta(seconds=1, microseconds=200)
        next_shipment.move_line_ids.date = next_shipment.date
        product._correct_inventory_valuation(next_shipment.date)
        self.assertEqual((shipment.value, next_shipment.value), (-40, -40))
        before_next = lots[1].with_context(to_date=at + timedelta(seconds=1, microseconds=100))
        self.assertEqual(before_next.product_qty, 1)
        self.assertEqual(before_next.total_value, 40)

    def test_unanchored_cost_and_cost_method_round_trip(self) -> None:
        at = self._opening_stock()
        with freeze_time(at):
            self.product_avco.standard_price = 40
        shipment = self._shipment(at + timedelta(seconds=1))
        self.assertEqual(shipment.value, -40)
        self.product_avco._correct_inventory_valuation(datetime.min)
        self.assertEqual(shipment.value, -40)
        checkpoint = self._checkpoint(shipment, 50)
        self.product_avco.categ_id.property_cost_method = "standard"
        self.product_avco.categ_id.property_cost_method = "average"
        self.product_avco._correct_inventory_valuation(datetime.min)
        self._assert_cold_value(at + timedelta(seconds=2), 2, 100)
        self.assertEqual(checkpoint.after_move_id, shipment)

    def test_position_survives_anchor_state_transition_and_history_reset(self) -> None:
        at = self._opening_stock()
        anchor = self._shipment(at)
        self._checkpoint(anchor)
        shipment = self._shipment(at + timedelta(seconds=1))
        self.assertEqual(shipment.value, -40)
        anchor.write({"state": "cancel"})
        self.assertFalse(anchor.is_out)
        self.assertEqual(shipment.value, -40)
        anchor.write({"state": "done"})
        self.assertEqual(anchor.value, -30)
        self.assertEqual(shipment.value, -40)
        self.env["product.value"].search([("product_id", "=", self.product_avco.id)]).unlink()
        self.assertFalse(self.env["product.value"].search([("product_id", "=", self.product_avco.id)]))
        self.product_avco._correct_inventory_valuation(datetime.min)
        self.assertEqual((anchor.value, shipment.value), (-30, -30))
        self._assert_cold_value(at + timedelta(seconds=1), 1, 30)

    def test_checkpoints_follow_anchor_order_instead_of_creation_order(self) -> None:
        at = self._opening_stock()
        first = self._shipment(at)
        second = self._shipment(at)
        self._checkpoint(second, 50)
        self._checkpoint(first, 40)
        self._assert_cold_value(at, 1, 50)
        self.assertEqual((first.value, second.value), (-30, -40))
        self.assertFalse(self.product_avco._correct_inventory_valuation(datetime.min))
        self._assert_cold_value(at, 1, 50)

    def test_zero_stock_checkpoint_does_not_override_later_receipt(self) -> None:
        at = datetime(2024, 1, 2, 12)
        self._receipt(at, 10)
        anchor = self._shipment(at + timedelta(seconds=1))
        self._checkpoint(anchor, 40)
        self._assert_cold_value(anchor.date, 0, 0)
        self._receipt(at + timedelta(seconds=2), 20)
        self.product_avco._correct_inventory_valuation(datetime.min)
        self._assert_cold_value(at + timedelta(seconds=2), 1, 20)
        self.assertEqual(anchor.value, -10)

    def test_unanchored_same_timestamp_cost_keeps_native_correction_order(self) -> None:
        at = datetime(2024, 1, 2, 12)
        for product in self.product_standard | self.product_avco | self.product_fifo:
            with self.subTest(method=product.cost_method):
                with freeze_time(at):
                    product.standard_price = 10
                    self._make_in_move(product, 3, unit_cost=10)
                with freeze_time(at + timedelta(seconds=1)):
                    product.standard_price = 40
                    if product.cost_method == "fifo":
                        self.env["product.value"].create(
                            {
                                "product_id": product.id,
                                "company_id": self.env.company.id,
                                "date": at + timedelta(seconds=1),
                                "value": 40,
                            }
                        )
                    shipment = self._make_out_move(product, 1)
                product._correct_inventory_valuation(datetime.min)
                self.assertEqual(shipment.value, -40)

    def test_reanchoring_replays_the_old_position(self) -> None:
        at = self._opening_stock()
        first = self._shipment(at)
        checkpoint = self._checkpoint(first)
        second = self._shipment(at)
        self.assertEqual(second.value, -40)
        checkpoint.after_move_id = second
        self.assertEqual((first.value, second.value), (-30, -30))
        self._assert_cold_value(at, 1, 40)
        checkpoint.unlink()
        self.assertEqual(self.product_avco.standard_price, 30)
        self._assert_cold_value(at, 1, 30)

    def test_batch_positions_keep_product_quantities_separate(self) -> None:
        at = self._opening_stock()
        first_product = self.product_avco
        first = self._shipment(at)
        self.product_avco = self.product_avco.copy()
        self._receipt(at - timedelta(seconds=1), 100)
        second = self._receipt(at, 100)
        self.env["product.value"].create([{"after_move_id": first.id, "value": 40}, {"after_move_id": second.id, "value": 50}])
        self.assertEqual((first_product.total_value, self.product_avco.total_value), (80, 100))

    def test_opening_quantity_uses_the_native_move_position(self) -> None:
        at = self._opening_stock()
        anchor = self._shipment(at)
        self._checkpoint(anchor)
        anchor.move_line_ids.date = at + timedelta(seconds=5)
        costs, values = self.product_avco._run_avco(at_date=at)
        self.assertEqual((costs[self.product_avco.id], values[self.product_avco.id]), (40, 80))
        self.assertEqual(anchor.move_line_ids.date, at + timedelta(seconds=5))

    def test_checkpoint_assignment_requires_a_completed_anchor(self) -> None:
        draft = self.env["stock.move"].create(
            {
                "product_id": self.product_avco.id,
                "uom_id": self.product_avco.uom_id.id,
                "product_uom_qty": 1,
                "location_id": self.stock_location.id,
                "location_dest_id": self.customer_location.id,
            }
        )
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._checkpoint(draft)

    def test_stock_user_can_validate_and_read_with_an_anchored_cost(self) -> None:
        at = self._opening_stock()
        self._checkpoint(self._shipment(at))
        self.env.invalidate_all()
        with freeze_time(at):
            shipment = (
                self.env["stock.move"]
                .with_user(self.inventory_user)
                .create(
                    {
                        "product_id": self.product_avco.id,
                        "uom_id": self.product_avco.uom_id.id,
                        "product_uom_qty": 1,
                        "location_id": self.stock_location.id,
                        "location_dest_id": self.customer_location.id,
                    }
                )
            )
            shipment._action_confirm()
            shipment._action_assign()
            shipment.quantity = 1
            shipment.picked = True
            shipment._action_done()
        self.assertEqual(shipment.value, -40)
        self.env.invalidate_all()
        product = self.product_avco.with_user(self.inventory_user)._with_valuation_context()
        costs, values = product._run_avco(at_date=at)
        self.assertEqual((costs[product.id], values[product.id]), (40, 40))

    def test_mixed_costs_share_current_seed_and_correction_order(self) -> None:
        original = self.product_avco
        for ordinary_first in (True, False):
            with self.subTest(ordinary_first=ordinary_first):
                self.product_avco = original.copy()
                at = self._opening_stock()
                if ordinary_first:
                    with freeze_time(at):
                        self.product_avco.standard_price = 35
                anchor = self._shipment(at)
                self._checkpoint(anchor)
                shipment = self._shipment(at)
                if not ordinary_first:
                    with freeze_time(at):
                        self.product_avco.standard_price = 35
                self.assertEqual((anchor.value, shipment.value), (-35, -40))
                self.assertEqual((self.product_avco.standard_price, self.product_avco.total_value), (40, 40))
                self._assert_cold_value(at, 1, 40)
                self.assertFalse(self.product_avco._correct_inventory_valuation(datetime.min))
                ordinary = self.env["product.value"].search(
                    [
                        ("product_id", "=", self.product_avco.id),
                        ("date", "=", at),
                        ("move_id", "=", False),
                        ("after_move_id", "=", False),
                    ]
                )
                moved = self.env["product.value"].create(
                    {"product_id": original.copy().id, "company_id": self.env.company.id, "date": at, "value": 36}
                )
                moved.write({"product_id": self.product_avco.id, "company_id": self.env.company.id})
                self.assertEqual((anchor.value, shipment.value), (-36, -40))
                moved.unlink()
                self.assertEqual((anchor.value, shipment.value), (-35, -40))
                ordinary.value = 36
                self.assertEqual((anchor.value, shipment.value), (-36, -40))
                self.assertEqual(self.product_avco.standard_price, 40)
                ordinary.unlink()
                self.assertEqual((anchor.value, shipment.value), (-30, -40))
                self.assertEqual(self.product_avco.standard_price, 40)
                self._assert_cold_value(at, 1, 40)

    def test_positioned_quantity_ignores_lot_and_warehouse_view_selectors(self) -> None:
        product = self.product_avco.copy({"tracking": "lot", "lot_valuated": False})
        lots = self.env["stock.lot"].create(
            [
                {"name": "Scoped quantity first lot", "product_id": product.id},
                {"name": "Scoped quantity second lot", "product_id": product.id},
            ]
        )
        at = datetime(2024, 1, 2, 12)
        with freeze_time(at):
            self._make_in_move(product, 2, unit_cost=10, lot_ids=lots[:1])
            self._make_in_move(product, 1, unit_cost=40, lot_ids=lots[1:])
            anchor = self._make_out_move(product, 1, lot_ids=lots[:1])
        self._checkpoint(anchor)
        with freeze_time(at + timedelta(seconds=1)):
            self._make_in_move(product, 1, unit_cost=50, lot_ids=lots[1:])
        warehouse = self.env["stock.warehouse"].sudo().create({"name": "Quantity scope warehouse", "code": "QSC"})
        cached_view = product.with_context(
            to_date=False, warehouse_id=False, search_warehouse=warehouse.id
        )._with_valuation_context()
        self.assertEqual(cached_view.qty_available, 0)
        scoped = product.with_context(lot_id=lots[0].id, search_warehouse=warehouse.id, search_location=warehouse.lot_stock_id.id)
        costs, values = scoped._run_avco(at_date=at)
        self.assertEqual((costs[product.id], values[product.id]), (40, 80))

    def test_global_lot_checkpoint_prices_each_first_later_shipment(self) -> None:
        product = self.product_avco.copy({"tracking": "lot", "lot_valuated": True})
        lots = self.env["stock.lot"].create(
            [
                {"name": "Global cost first lot", "product_id": product.id},
                {"name": "Global cost second lot", "product_id": product.id},
            ]
        )
        at = datetime(2024, 1, 2, 12)
        with freeze_time(at):
            self._make_in_move(product, 2, unit_cost=10, lot_ids=lots[:1])
            anchor = self._make_in_move(product, 2, unit_cost=100, lot_ids=lots[1:])
        warehouse = self.env["stock.warehouse"].sudo().create({"name": "Global price view warehouse", "code": "GPV"})
        checkpoint = (
            self.env["product.value"].with_context(search_warehouse=warehouse.id).create({"after_move_id": anchor.id, "value": 40})
        )
        self.assertEqual(product.standard_price, 40)
        self.assertEqual(product.with_context(to_date=False, warehouse_id=False).avg_cost, 40)
        checkpoint.value = 45
        self.assertEqual(product.standard_price, 45)
        checkpoint.value = 40
        self.assertEqual(product.standard_price, 40)
        self.assertEqual(lots.mapped("standard_price"), [40, 40])
        with freeze_time(at + timedelta(seconds=1)):
            first = self._make_out_move(product, 1, lot_ids=lots[:1])
            second = self._make_out_move(product, 1, lot_ids=lots[1:])
        self.assertEqual((first.value, second.value), (-40, -40))
        self.assertFalse(product._correct_inventory_valuation(datetime.min))
        self.env.invalidate_all()
        self.assertEqual((first.value, second.value), (-40, -40))

    def test_native_reset_and_revalidation_move_the_checkpoint_with_its_anchor(self) -> None:
        at = self._opening_stock()
        anchor = self._shipment(at)
        checkpoint = self._checkpoint(anchor)
        shipment = self._shipment(at + timedelta(seconds=1))
        self.assertEqual(shipment.value, -40)
        anchor._action_reset_to_progress()
        with freeze_time(at + timedelta(seconds=2)):
            anchor.quantity = 1
            anchor.picked = True
            anchor._action_done()
        self.assertEqual(checkpoint.date, anchor.date)
        self.assertEqual(anchor.date, at + timedelta(seconds=2))
        self.assertEqual((anchor.value, shipment.value), (-30, -30))
        self.assertEqual((self.product_avco.standard_price, self.product_avco.total_value), (40, 40))
        self.assertFalse(self.product_avco._correct_inventory_valuation(datetime.min))
        self._assert_cold_value(at + timedelta(seconds=3), 1, 40)

    def test_new_associations_require_supported_cost_and_lot_modes(self) -> None:
        at = datetime(2024, 1, 2, 12)
        with freeze_time(at):
            standard = self._make_in_move(self.product_standard, 1, unit_cost=10)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._checkpoint(standard)
        at = self._opening_stock()
        anchor = self._shipment(at)
        lot = self.env["stock.lot"].create({"name": "Unvalued checkpoint lot", "product_id": self.product_avco.id})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.env["product.value"].create({"after_move_id": anchor.id, "lot_id": lot.id, "value": 40})

    def test_ordinary_seeds_use_owned_valued_quantity(self) -> None:
        at = datetime(2024, 1, 2, 12)
        owner = self.env["res.partner"].create({"name": "Ordinary seed consignment"})
        for lot_valuated in (False, True):
            with self.subTest(lot_valuated=lot_valuated), self.cr.savepoint():
                product = self.product_avco.copy({"tracking": "lot" if lot_valuated else False, "lot_valuated": lot_valuated})
                lot = (
                    self.env["stock.lot"].create({"name": "Ordinary seed lot", "product_id": product.id}) if lot_valuated else False
                )
                move_values = {"lot_ids": lot} if lot else {}
                for index, cost in enumerate((10, 30, 50)):
                    with freeze_time(at + timedelta(seconds=index)):
                        self._make_in_move(product, 1, unit_cost=cost, **move_values)
                with freeze_time(at + timedelta(seconds=3)):
                    self._make_in_move(product, 7, unit_cost=999, owner_id=owner.id, **move_values)
                seed_date = at + timedelta(seconds=4)
                seed_values = {"product_id": product.id, "company_id": self.env.company.id, "date": seed_date, "value": 20}
                if lot:
                    seed_values["lot_id"] = lot.id
                with freeze_time(seed_date):
                    self.env["product.value"].create(seed_values)
                with freeze_time(at + timedelta(seconds=5)):
                    self._make_in_move(product, 1, unit_cost=100, **move_values)
                with freeze_time(at + timedelta(seconds=6)):
                    shipment = self._make_out_move(product, 1, **move_values)
                self.assertEqual(shipment.value, -40)
                self.assertEqual((lot or product).standard_price, 40)
                self.assertFalse(product._correct_inventory_valuation(seed_date))
                self.env.invalidate_all()
                self.assertEqual(shipment.value, -40)
                scoped_product = (
                    product._with_company_stock_context()._with_valuation_context().with_context(to_date=at + timedelta(seconds=6))
                )
                scoped = lot.with_env(scoped_product.env) if lot else scoped_product
                self.assertEqual(scoped["product_qty" if lot else "qty_available"], 3)
                self.assertEqual(scoped.total_value, 120)
