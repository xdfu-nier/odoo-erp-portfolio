"""Physical facts stay native while fulfillment policy closes public ORM bypasses."""
import unittest

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "portfolio_stock")
class TestStockFulfillment(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create({
            "name": "Physical fulfillment A", "portfolio_fulfillment_enabled": True,
        })
        cls.other_company = cls.env["res.company"].create({"name": "Native stock B"})
        cls.companies = cls.company | cls.other_company
        # Native inventory uses sudo without changing uid. Fixture membership is
        # real, including the runner actor; it is never supplied as authorization.
        cls.env.user.write({"company_ids": [Command.link(company.id) for company in cls.companies]})
        cls.env = cls.env(context={**cls.env.context, "allowed_company_ids": cls.companies.ids})
        actors = (
            ("sales", "sales_team.group_sale_salesman", cls.companies),
            ("buyer", "purchase.group_purchase_user", cls.companies),
            ("finance", "account.group_account_invoice", cls.companies),
            ("warehouse", "stock.group_stock_user", cls.companies),
            ("stock_manager", "stock.group_stock_manager", cls.companies),
            ("sales_stock", "sales_team.group_sale_salesman,stock.group_stock_user", cls.companies),
            ("reviewer", "sales_team.group_sale_manager", cls.companies),
            ("outsider", "stock.group_stock_user", cls.other_company),
        )
        for name, groups, companies in actors:
            setattr(cls, name, new_test_user(
                cls.env, login=f"physical_{name}", groups=groups,
                company_id=companies[0].id, company_ids=[Command.set(companies.ids)],
            ))
        cls.warehouse_a = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.warehouse_b = cls.env["stock.warehouse"].search([("company_id", "=", cls.other_company.id)], limit=1)
        cls.location = cls.warehouse_a.lot_stock_id
        cls.other_location = cls.warehouse_b.lot_stock_id
        cls.empty_location = cls.env["stock.location"].create({
            "name": "Empty internal source", "usage": "internal", "company_id": cls.company.id,
            "location_id": cls.warehouse_a.view_location_id.id,
        })
        cls.customer_location = cls.env.ref("stock.stock_location_customers")
        cls.supplier_location = cls.env.ref("stock.stock_location_suppliers")
        cls.customer = cls.env["res.partner"].create({"name": "Physical customer", "ref": "PHYS-C"})
        cls.vendor = cls.env["res.partner"].create({"name": "Physical vendor", "ref": "PHYS-V"})
        cls.owner = cls.env["res.partner"].create({"name": "Other stock owner", "ref": "PHYS-OWNER"})
        cls.product = cls.env["product.product"].create({
            "name": "Physical bearing", "default_code": "PHYS-BRG", "type": "consu",
            "is_storable": True, "invoice_policy": "delivery", "list_price": 100,
            "standard_price": 60, "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
        })
        cls.env["product.supplierinfo"].create({
            "product_tmpl_id": cls.product.product_tmpl_id.id, "partner_id": cls.vendor.id,
            "company_id": cls.company.id, "price": 60, "currency_id": cls.company.currency_id.id,
        })
        for company, location in ((cls.company, cls.location), (cls.other_company, cls.other_location)):
            quant = cls.env["stock.quant"].with_user(cls.stock_manager).with_company(company).with_context(
                inventory_mode=True,
            ).create({"product_id": cls.product.id, "location_id": location.id, "inventory_quantity": 40})
            quant.action_apply_inventory()
        cls.source = cls.make_sale(cls, quantity=100)
        cls.source.action_confirm()
        cls.purchase = cls.env["purchase.order"].with_user(cls.buyer).with_company(cls.company).create({
            "partner_id": cls.vendor.id, "company_id": cls.company.id,
            "portfolio_source_sale_id": cls.source.id,
            "order_line": [Command.create({
                "product_id": cls.product.id, "product_qty": 60, "price_unit": 60,
                "product_uom": cls.product.uom_po_id.id, "date_planned": fields.Datetime.now(),
                "taxes_id": [Command.clear()],
            })],
        })
        cls.purchase.button_confirm()

    def make_sale(self, quantity=1, user=None, **values):
        user = user or self.sales
        return self.env["sale.order"].with_user(user).with_company(self.company).create({
            "partner_id": self.customer.id, "company_id": self.company.id, "user_id": user.id,
            "order_line": [Command.create({
                "product_id": self.product.id, "product_uom_qty": quantity, "price_unit": 100,
                "discount": 15, "tax_id": [Command.clear()],
            })], **values,
        })

    def stock(self, location=None, product=None, **identity):
        domain = [("product_id", "=", (product or self.product).id),
                  ("location_id", "=", (location or self.location).id)]
        domain += [(key, "=", value.id if value else False) for key, value in identity.items()]
        return sum(self.env["stock.quant"].search(domain).mapped("quantity"))

    def new_product(self, **values):
        product = self.env["product.product"].create({
            "name": "Another physical bearing", "default_code": "PHYS-ANOTHER", "type": "consu",
            "is_storable": True, "invoice_policy": "delivery", "list_price": 100,
            "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()], **values,
        })
        self.assertTrue(product.id)
        return product

    def facts(self):
        self.env.flush_all()
        self.env.invalidate_all()
        moves = self.env["stock.move"].search([("company_id", "in", self.companies.ids)], order="id")
        lines = self.env["stock.move.line"].search([("company_id", "in", self.companies.ids)], order="id")
        products = self.product | moves.product_id | lines.product_id
        return {
            "quants": self.env["stock.quant"].search([
                ("product_id", "in", products.ids),
            ], order="id").read([
                "product_id", "location_id", "lot_id", "package_id", "owner_id", "quantity", "reserved_quantity",
            ]),
            "moves": moves.read([
                "state", "product_id", "product_uom_qty", "quantity", "product_uom", "company_id",
                "location_id", "location_dest_id", "picking_id", "sale_line_id", "origin_returned_move_id", "to_refund",
            ]),
            "lines": lines.read([
                "state", "quantity", "quantity_product_uom", "product_id", "product_uom_id", "company_id", "location_id", "location_dest_id",
                "lot_id", "package_id", "result_package_id", "owner_id", "move_id", "picking_id",
            ]),
            "pickings": moves.picking_id.sorted("id").read([
                "state", "date_done", "company_id", "location_id", "location_dest_id",
            ]),
        }

    def validate(self, picking, quantity, user=None, expect_backorder=False):
        picking = picking.with_user(user or self.warehouse).with_company(picking.company_id)
        if quantity is not None:
            picking.move_ids.write({"quantity": quantity, "picked": True})
        result = picking.button_validate()
        if isinstance(result, dict) and result.get("res_model") == "stock.backorder.confirmation":
            wizard = self.env["stock.backorder.confirmation"].with_user(user or self.warehouse).with_context(
                **result["context"],
            ).create({})
            wizard.process()
        elif expect_backorder:
            self.fail(f"Expected the native backorder wizard, got {result!r}")
        self.assertEqual(picking.state, "done")
        return self.env["stock.picking"].search([("backorder_id", "=", picking.id)])

    def move(self, quantity=1, company=None, source=None, destination=None, product=None):
        company = company or self.company
        product = product or self.product
        move = self.env["stock.move"].with_user(self.warehouse).with_company(company).create({
            "name": "Physical transfer", "company_id": company.id, "product_id": product.id,
            "product_uom": product.uom_id.id, "product_uom_qty": quantity,
            "location_id": (source or self.location).id,
            "location_dest_id": (destination or self.customer_location).id,
        })
        move._action_confirm(merge=False)
        move.write({"quantity": quantity, "picked": True})
        return move

    def done(self):
        picking = self.source.picking_ids
        self.validate(picking, 10, expect_backorder=True)
        return picking.move_ids.filtered(lambda move: move.state == "done")

    def assert_blocked_unchanged(self, operation, exception=UserError):
        before = self.facts()
        with self.cr.savepoint():
            # Odoo's assertRaises automatically rolls back, which would hide a
            # partial physical update. Catch with stdlib, then inspect the live
            # transaction before this outer isolation savepoint exits.
            self.env.invalidate_all()
            with unittest.TestCase.assertRaises(self, exception):
                operation()
            self.assertEqual(self.facts(), before)

    def assert_safe_native_defaults(self, operation, verify):
        """A forged default may be rejected atomically or normalized privately."""
        before = self.facts()
        with self.cr.savepoint():
            self.env.invalidate_all()
            try:
                result = operation()
            except UserError:
                self.assertEqual(self.facts(), before)
            else:
                self.env.flush_all()
                self.env.invalidate_all()
                verify(result)

    def test_receipt_delivery_backorders_preserve_native_records_and_quantities(self):
        self.assertFalse(self.company.portfolio_approval_enabled)
        self.assertFalse(self.company.portfolio_masterdata_enabled)
        self.assertEqual(self.stock(), 40)
        self.assertFalse(self.purchase.picking_ids.move_ids.sale_line_id)
        receipt = self.purchase.picking_ids
        receipt_backorder = self.validate(receipt, 30, expect_backorder=True)
        self.assertEqual(self.stock(), 70)
        delivery = self.source.picking_ids
        delivery_backorder = self.validate(delivery, 70, expect_backorder=True)
        self.assertEqual(self.stock(), 0)
        self.assertEqual(delivery_backorder.move_ids.product_uom_qty, 30)
        self.validate(receipt_backorder, 30)
        self.assertEqual(self.stock(), 30)
        self.validate(delivery_backorder, 30)
        self.assertEqual(self.stock(), 0)
        self.assertEqual(self.source.order_line.qty_delivered, 100)
        self.assertEqual(sum(self.purchase.order_line.mapped("qty_received")), 60)
        self.assertEqual(len(self.source.picking_ids), 2)
        self.assertEqual(len(self.purchase.picking_ids), 2)
        self.assertTrue(all(self.source.picking_ids.move_ids.mapped("move_line_ids")))

    def test_full_m1_approval_coexists_with_physical_fulfillment(self):
        self.company.write({"portfolio_approval_enabled": True, "portfolio_masterdata_enabled": True})
        order = self.make_sale(quantity=100)
        order.action_portfolio_submit()
        order.with_user(self.reviewer).action_portfolio_approve()
        order.action_confirm()
        self.validate(order.picking_ids, 40, expect_backorder=True)
        self.assertEqual(order.order_line.qty_delivered, 40)

    def test_own_reserved_stock_is_not_subtracted_twice(self):
        self.assertEqual(sum(self.env["stock.quant"].search([
            ("location_id", "=", self.location.id), ("product_id", "=", self.product.id),
        ]).mapped("reserved_quantity")), 40)
        self.validate(self.source.picking_ids, 40, expect_backorder=True)
        self.assertEqual(self.stock(), 0)

    def test_warehouse_stock_user_completes_without_sales_business_write_grants(self):
        self.assertFalse(self.warehouse.has_group("sales_team.group_sale_salesman"))
        self.validate(self.source.picking_ids, 10, expect_backorder=True)
        self.assertEqual(self.source.order_line.qty_delivered, 10)

    def test_combined_own_sales_stock_actor_can_complete_another_salespersons_transfer(self):
        self.assertNotEqual(self.source.user_id, self.sales_stock)
        with self.assertRaises(AccessError):
            self.source.with_user(self.sales_stock).write({"note": "Outside own sales scope"})
        self.validate(self.source.picking_ids, 10, user=self.sales_stock, expect_backorder=True)
        self.assertEqual(self.stock(), 30)

    def test_standard_inventory_adjustment_remains_native(self):
        quant = self.env["stock.quant"].with_user(self.warehouse).search([
            ("location_id", "=", self.location.id), ("product_id", "=", self.product.id),
        ], limit=1)
        quant.with_context(inventory_mode=True).write({"inventory_quantity": 43})
        quant.action_apply_inventory()
        self.assertEqual(self.stock(), 43)
        self.assertTrue(self.env["stock.move"].search([
            ("product_id", "=", self.product.id), ("is_inventory", "=", True), ("state", "=", "done"),
        ]))

    def test_stock_manager_count_to_zero_applies_native_inventory_move(self):
        quant = self.env["stock.quant"].with_user(self.stock_manager).search([
            ("location_id", "=", self.location.id), ("product_id", "=", self.product.id),
        ])
        quant.with_context(inventory_mode=True).write({"inventory_quantity": 0})
        quant.action_apply_inventory()
        self.assertEqual(self.stock(), 0)
        self.assertTrue(self.env["stock.move"].search([
            ("product_id", "=", self.product.id), ("location_id", "=", self.location.id),
            ("is_inventory", "=", True), ("state", "=", "done"),
        ]))

    def test_nonzero_quant_sudo_unlink_cannot_remove_physical_facts(self):
        quant = self.env["stock.quant"].search([
            ("location_id", "=", self.location.id), ("product_id", "=", self.product.id),
        ])
        self.assert_blocked_unchanged(lambda: quant.with_user(self.stock_manager).sudo().unlink())

    def test_native_zero_quant_cleanup_is_allowed_under_sudo(self):
        move = self.move(quantity=40)
        move._action_done()
        self.env["stock.quant"].with_user(self.warehouse).sudo()._unlink_zero_quants()
        self.assertEqual(self.stock(), 0)

    def test_disabled_company_keeps_native_negative_stock_compatibility(self):
        move = self.move(quantity=41, company=self.other_company, source=self.other_location)
        move.with_user(self.sales)._action_done()
        self.assertEqual(move.state, "done")
        self.assertEqual(self.stock(self.other_location), -1)

    def test_compatible_nonunit_uom_uses_product_stock_units(self):
        dozen = self.env.ref("uom.product_uom_dozen")
        move = self.env["stock.move"].with_user(self.warehouse).with_company(self.company).create({
            "name": "Two dozen bearings", "company_id": self.company.id, "product_id": self.product.id,
            "product_uom": dozen.id, "product_uom_qty": 2,
            "location_id": self.location.id, "location_dest_id": self.customer_location.id,
        })
        move._action_confirm(merge=False)
        move.write({"quantity": 2, "picked": True})
        move._action_done()
        self.assertEqual(self.stock(), 16)

    def test_native_serial_requires_lot_and_accepts_valid_serial(self):
        product = self.new_product(name="Serial bearing", default_code="PHYS-SER", tracking="serial")
        move = self.move(product=product, source=self.supplier_location, destination=self.location)
        self.assert_blocked_unchanged(lambda: move._action_done())
        lot = self.env["stock.lot"].with_user(self.warehouse).create({
            "name": "PHYS-SERIAL-1", "product_id": product.id, "company_id": self.company.id,
        })
        move.move_line_ids.write({"lot_id": lot.id})
        move._action_done()
        self.assertEqual(self.stock(product=product), 1)

    def test_native_uom_rounding_validation_is_preserved(self):
        self.product.uom_id.rounding = 1
        with self.assertRaises(UserError), self.cr.savepoint():
            self.move(quantity=0.5)._action_done()

    def test_overship_41_from_40_is_rolled_back_on_standard_validate(self):
        self.source.picking_ids.with_user(self.warehouse).move_ids.write({"quantity": 41, "picked": True})
        self.assert_blocked_unchanged(lambda: self.validate(self.source.picking_ids, None, expect_backorder=True))
        self.assertNotEqual(self.source.picking_ids.state, "done")
        self.assertEqual(self.stock(), 40)

    def test_forged_move_line_base_quantity_cannot_ship_without_moving_physical_stock(self):
        picking = self.source.picking_ids.with_user(self.warehouse)
        picking.move_ids.write({"quantity": 41, "picked": True})
        self.env.flush_all()
        self.assertEqual(sum(picking.move_line_ids.mapped("quantity_product_uom")), 41)
        before = self.facts()
        with self.cr.savepoint():
            self.env.invalidate_all()
            try:
                picking.move_line_ids.write({"quantity_product_uom": 0})
            except UserError:
                self.assertEqual(self.facts(), before)
            else:
                try:
                    self.validate(picking, None, expect_backorder=True)
                except UserError:
                    self.env.flush_all()
                    self.env.invalidate_all()
                    self.assertEqual(self.stock(), 40)
                    self.assertNotEqual(picking.state, "done")
                    self.assertEqual(self.source.order_line.qty_delivered, 0)
                else:
                    self.fail(
                        "Forged base quantity completed a delivery: "
                        f"state={picking.state}, move_quantity={picking.move_ids.quantity}, "
                        f"delivered={self.source.order_line.qty_delivered}, physical_stock={self.stock()}"
                    )

    def test_legacy_move_line_base_quantity_is_rechecked_before_native_quant_deltas(self):
        picking = self.source.picking_ids.with_user(self.warehouse)
        picking.move_ids.write({"quantity": 41, "picked": True})
        self.env.flush_all()
        self.company.portfolio_fulfillment_enabled = False
        picking.move_line_ids.write({"quantity_product_uom": 0})
        self.env.flush_all()
        self.company.portfolio_fulfillment_enabled = True
        self.assertEqual(sum(picking.move_line_ids.mapped("quantity_product_uom")), 0)
        self.assert_blocked_unchanged(lambda: self.validate(picking, None, expect_backorder=True))
        self.assertNotEqual(picking.state, "done")
        self.assertEqual(self.source.order_line.qty_delivered, 0)
        self.assertEqual(self.stock(), 40)

    def test_public_move_line_base_quantity_inputs_and_defaults_are_rejected(self):
        move = self.move()
        self.assert_blocked_unchanged(lambda: move.move_line_ids.write({"quantity_product_uom": 0}))
        model = self.env["stock.move.line"].with_user(self.warehouse).with_company(self.company)
        values = {
            "company_id": self.company.id, "product_id": self.product.id,
            "product_uom_id": self.product.uom_id.id, "move_id": move.id,
            "location_id": self.location.id, "location_dest_id": self.customer_location.id,
            "quantity": 1,
        }
        for origin in ("explicit", "context", "ir.default"):
            with self.subTest(origin=origin):
                candidate = model
                extra = {}
                if origin == "explicit":
                    extra["quantity_product_uom"] = 0
                elif origin == "context":
                    candidate = model.with_context(default_quantity_product_uom=0)
                else:
                    self.env["ir.default"].set("stock.move.line", "quantity_product_uom", 0, user_id=self.warehouse.id)
                self.assert_blocked_unchanged(lambda: candidate.create({**values, **extra}))

    def test_move_line_cannot_fulfill_sales_product_by_consuming_a_different_product(self):
        other = self.new_product(name="Different physical bearing", default_code="PHYS-DIFFERENT")
        quant = self.env["stock.quant"].with_user(self.warehouse).with_company(self.company).with_context(
            inventory_mode=True,
        ).create({"product_id": other.id, "location_id": self.location.id, "inventory_quantity": 1})
        quant.action_apply_inventory()
        picking = self.source.picking_ids.with_user(self.warehouse)
        picking.move_line_ids.unlink()
        picking.move_ids.write({"picked": True})
        before = self.facts()
        with self.cr.savepoint():
            self.env.invalidate_all()
            try:
                self.env["stock.move.line"].with_user(self.warehouse).create({
                    "move_id": picking.move_ids.id, "picking_id": picking.id,
                    "product_id": other.id, "product_uom_id": other.uom_id.id,
                    "location_id": self.location.id, "location_dest_id": self.customer_location.id,
                    "quantity": 1, "picked": True,
                })
            except UserError:
                self.assertEqual(self.facts(), before)
            else:
                try:
                    self.validate(picking, None, expect_backorder=True)
                except UserError:
                    self.env.flush_all()
                    self.env.invalidate_all()
                    self.assertNotEqual(picking.state, "done")
                    self.assertEqual(self.stock(), 40)
                    self.assertEqual(self.stock(product=other), 1)
                    self.assertEqual(self.source.order_line.qty_delivered, 0)
                else:
                    self.fail(
                        "A different product fulfilled the sales source: "
                        f"delivered={self.source.order_line.qty_delivered}, "
                        f"ordered_product_stock={self.stock()}, other_product_stock={self.stock(product=other)}"
                    )

    def test_public_batch_validation_rolls_back_both_native_backorder_cohorts(self):
        first = self.make_sale(quantity=30)
        second = self.make_sale(quantity=30)
        (first | second).action_confirm()
        never = self.warehouse_a.out_type_id.copy({
            "name": "Physical never-backorder deliveries", "sequence_code": "PHYS-NEVER",
            "create_backorder": "never",
        })
        self.warehouse_a.out_type_id.create_backorder = "ask"
        first.picking_ids.write({"picking_type_id": never.id})
        pickings = (first.picking_ids | second.picking_ids).with_user(self.warehouse)
        pickings.move_ids.write({"quantity": 30, "picked": True})
        self.assertEqual(sum(pickings.move_ids.mapped("quantity")), 60)
        self.assertEqual(self.stock(), 40)
        self.assert_blocked_unchanged(lambda: pickings.button_validate())
        self.assertFalse(pickings.filtered(lambda picking: picking.state == "done"))
        self.assertEqual(self.stock(), 40)

    def test_multicompany_batch_cannot_validate_a_move_against_another_company_sales_line(self):
        self.other_company.portfolio_fulfillment_enabled = True
        source = self.make_sale(
            quantity=1, company_id=self.other_company.id, warehouse_id=self.warehouse_b.id,
        )
        source.action_confirm()
        legitimate = source.picking_ids
        forged = self.env["stock.picking"].with_user(self.warehouse).with_company(self.company).create({
            "company_id": self.company.id, "picking_type_id": self.warehouse_a.out_type_id.id,
            "location_id": self.location.id, "location_dest_id": self.customer_location.id,
            "move_ids": [Command.create({
                "name": "Company A stock with company B sales provenance", "company_id": self.company.id,
                "product_id": self.product.id, "product_uom": self.product.uom_id.id,
                "product_uom_qty": 1, "location_id": self.location.id,
                "location_dest_id": self.customer_location.id,
            })],
        })
        pickings = (forged | legitimate).with_user(self.warehouse).with_context(
            allowed_company_ids=self.companies.ids,
        )
        # Prepare ordinary native inputs before attaching the bad provenance;
        # quantity inverses legitimately touch individual move lines.
        pickings.move_ids.write({"quantity": 1, "picked": True})
        # Model an existing native relation from before policy activation. The
        # completion gate must still validate each move's actual source company.
        self.companies.write({"portfolio_fulfillment_enabled": False})
        pickings.move_ids.write({"sale_line_id": source.order_line.id})
        self.companies.write({"portfolio_fulfillment_enabled": True})
        self.assertEqual(set(pickings.company_id.ids), set(self.companies.ids))
        self.assertEqual(forged.move_ids.sale_line_id.order_id.company_id, self.other_company)
        self.assertEqual(source.order_line.qty_delivered, 0)
        self.assert_blocked_unchanged(lambda: pickings.button_validate())
        self.assertFalse(pickings.filtered(lambda picking: picking.state == "done"))
        self.assertEqual(self.stock(), 40)
        self.assertEqual(self.stock(self.other_location), 40)
        self.assertEqual(source.order_line.qty_delivered, 0)

    def test_pending_move_cannot_attach_a_foreign_enabled_sales_source(self):
        self.other_company.portfolio_fulfillment_enabled = True
        source = self.make_sale(
            quantity=1, company_id=self.other_company.id, warehouse_id=self.warehouse_b.id,
        )
        source.action_confirm()
        for stock_policy in (True, False):
            self.company.portfolio_fulfillment_enabled = stock_policy
            with self.subTest(stock_company_enabled=stock_policy, origin="write"):
                move = self.move()
                self.assertFalse(move.sale_line_id)
                self.assert_blocked_unchanged(lambda: move.write({"sale_line_id": source.order_line.id}))
                self.assertFalse(move.sale_line_id)
            for origin in ("context", "ir.default"):
                with self.subTest(stock_company_enabled=stock_policy, origin=origin):
                    model = self.env["stock.move"].with_user(self.warehouse).with_company(self.company)
                    if origin == "context":
                        model = model.with_context(default_sale_line_id=source.order_line.id)
                    else:
                        self.env["ir.default"].set("stock.move", "sale_line_id", source.order_line.id, user_id=self.warehouse.id)
                    try:
                        self.assert_blocked_unchanged(lambda: model.create({
                            "name": "Foreign sales source from effective default", "company_id": self.company.id,
                            "product_id": self.product.id, "product_uom": self.product.uom_id.id,
                            "product_uom_qty": 1, "location_id": self.location.id,
                            "location_dest_id": self.customer_location.id,
                        }))
                    finally:
                        if origin == "ir.default":
                            self.env["ir.default"].set("stock.move", "sale_line_id", False, user_id=self.warehouse.id)

    def test_legitimate_multicompany_batch_preserves_each_move_sales_source(self):
        self.other_company.portfolio_fulfillment_enabled = True
        source_a = self.make_sale(quantity=1)
        source_b = self.make_sale(
            quantity=1, company_id=self.other_company.id, warehouse_id=self.warehouse_b.id,
        )
        (source_a | source_b).action_confirm()
        pickings = (source_a.picking_ids | source_b.picking_ids).with_user(self.warehouse)
        pickings.move_ids.write({"quantity": 1, "picked": True})
        pickings.button_validate()
        self.assertTrue(all(picking.state == "done" for picking in pickings))
        self.assertEqual(self.stock(), 39)
        self.assertEqual(self.stock(self.other_location), 39)
        self.assertEqual(source_a.order_line.qty_delivered, 1)
        self.assertEqual(source_b.order_line.qty_delivered, 1)

    def test_multiple_lines_cannot_each_spend_the_same_stock(self):
        move = self.move(quantity=42)
        move.move_line_ids.unlink()
        self.env["stock.move.line"].with_user(self.warehouse).create([{
            "move_id": move.id, "product_id": self.product.id, "product_uom_id": self.product.uom_id.id,
            "location_id": self.location.id, "location_dest_id": self.customer_location.id,
            "quantity": 21, "picked": True,
        } for _ in range(2)])
        self.assert_blocked_unchanged(lambda: move._action_done())

    def test_different_products_cannot_cover_an_unavailable_product(self):
        product = self.new_product(name="Empty bearing", default_code="PHYS-EMPTY")
        move = self.move(product=product)
        self.assert_blocked_unchanged(lambda: move._action_done())

    def test_internal_transfer_from_empty_location_cannot_invent_stock(self):
        move = self.move(source=self.empty_location, destination=self.location)
        self.assert_blocked_unchanged(lambda: move._action_done())
        self.assertEqual(self.stock(self.empty_location), 0)

    def test_other_owner_stock_does_not_cover_unowned_shipment(self):
        move = self.move()
        move.move_line_ids.write({"owner_id": self.owner.id})
        self.assert_blocked_unchanged(lambda: move._action_done())

    def test_other_package_stock_does_not_cover_requested_package(self):
        package = self.env["stock.quant.package"].with_user(self.warehouse).create({"name": "Empty package"})
        move = self.move()
        move.move_line_ids.write({"package_id": package.id})
        self.assert_blocked_unchanged(lambda: move._action_done())

    def test_other_lot_stock_does_not_cover_requested_lot(self):
        product = self.new_product(name="Lot bearing", default_code="PHYS-LOT", tracking="lot")
        lots = self.env["stock.lot"].with_user(self.warehouse).create([{
            "name": name, "product_id": product.id, "company_id": self.company.id,
        } for name in ("PHYS-LOT-A", "PHYS-LOT-B")])
        receipt = self.move(quantity=2, product=product, source=self.supplier_location, destination=self.location)
        receipt.move_line_ids.write({"lot_id": lots[0].id})
        receipt._action_done()
        move = self.move(product=product)
        move.move_line_ids.write({"lot_id": lots[1].id})
        self.assert_blocked_unchanged(lambda: move._action_done())
        self.assertEqual(self.stock(product=product), 2)

    def test_sales_buyer_finance_cannot_complete_moves_even_under_sudo(self):
        move = self.move()
        for user in (self.sales, self.buyer, self.finance):
            for sudo in (False, True):
                with self.subTest(actor=user.login, sudo=sudo):
                    self.assert_blocked_unchanged(
                        lambda user=user, sudo=sudo: move.with_user(user).sudo(sudo)._action_done(), AccessError,
                    )

    def test_sales_cannot_use_public_validate_to_complete_physical_stock(self):
        self.assertTrue(self.source.picking_ids.with_user(self.sales).has_access("write"))
        picking = self.source.picking_ids
        picking.with_user(self.warehouse).move_ids.write({"quantity": 10, "picked": True})
        self.assert_blocked_unchanged(
            lambda: picking.with_user(self.sales).with_context(skip_backorder=True).button_validate(), AccessError,
        )

    def test_actual_company_membership_is_required_despite_sudo_and_allowed_context(self):
        move = self.move()
        self.assert_blocked_unchanged(lambda: move.with_user(self.outsider).sudo().with_context(
            allowed_company_ids=self.companies.ids, portfolio_stock_internal=True,
        )._action_done(), AccessError)

    def test_direct_move_line_completion_cannot_bypass_parent_operation(self):
        move = self.move()
        self.assert_blocked_unchanged(lambda: move.move_line_ids._action_done(), AccessError)

    def test_direct_quant_primitive_cannot_bypass_parent_operation(self):
        self.assert_blocked_unchanged(lambda: self.env["stock.quant"].with_user(self.warehouse)._update_available_quantity(
            self.product, self.location, quantity=99,
        ), AccessError)

    def test_public_done_move_create_and_context_defaults_are_rejected(self):
        values = {
            "name": "Forged done", "company_id": self.company.id, "product_id": self.product.id,
            "product_uom": self.product.uom_id.id, "product_uom_qty": 1,
            "location_id": self.location.id, "location_dest_id": self.customer_location.id,
        }
        model = self.env["stock.move"].with_user(self.warehouse).with_company(self.company)
        for context, extra in (({}, {"state": "done"}), ({"default_state": "done"}, {})):
            with self.subTest(context=context):
                self.assert_blocked_unchanged(lambda context=context, extra=extra: model.with_context(**context).create({
                    **values, **extra,
                }))

    def test_public_done_move_ir_default_is_rejected(self):
        self.env["ir.default"].set("stock.move", "state", "done", user_id=self.warehouse.id)
        self.assert_blocked_unchanged(lambda: self.move())

    def test_public_done_state_write_rejects_forged_context_capabilities(self):
        move = self.move()
        self.assert_blocked_unchanged(lambda: move.with_context(
            portfolio_stock_internal=True, portfolio_stock_capability=True, inventory_mode=True,
        ).write({"state": "done"}))

    def test_public_picking_done_state_cannot_complete_its_moves(self):
        picking = self.source.picking_ids.with_user(self.warehouse)
        self.assert_blocked_unchanged(lambda: picking.write({"state": "done"}))

    def test_public_done_picking_create_and_context_default_are_rejected(self):
        model = self.env["stock.picking"].with_user(self.warehouse).with_company(self.company)
        values = {
            "picking_type_id": self.warehouse_a.out_type_id.id,
            "location_id": self.location.id, "location_dest_id": self.customer_location.id,
        }
        for context, extra in (({}, {"state": "done"}), ({"default_state": "done"}, {})):
            with self.subTest(context=context):
                self.assert_blocked_unchanged(lambda context=context, extra=extra: model.with_context(**context).create({
                    **values, **extra,
                }))

    def test_public_done_picking_ir_default_is_rejected(self):
        self.env["ir.default"].set("stock.picking", "state", "done", user_id=self.warehouse.id)
        self.assert_blocked_unchanged(lambda: self.env["stock.picking"].with_user(self.warehouse).with_company(
            self.company,
        ).create({
            "company_id": self.company.id, "picking_type_id": self.warehouse_a.out_type_id.id,
            "location_id": self.location.id, "location_dest_id": self.customer_location.id,
        }))

    def test_public_done_line_state_and_default_cannot_create_physical_stock(self):
        move = self.move()
        values = {
            "move_id": move.id, "product_id": self.product.id, "product_uom_id": self.product.uom_id.id,
            "location_id": self.location.id, "location_dest_id": self.customer_location.id,
            "quantity": 1, "picked": True,
        }
        model = self.env["stock.move.line"].with_user(self.warehouse)
        for context, extra in (({}, {"state": "done"}), ({"default_state": "done"}, {})):
            with self.subTest(context=context):
                self.assert_blocked_unchanged(lambda context=context, extra=extra: model.with_context(**context).create({
                    **values, **extra,
                }))
        self.env["ir.default"].set("stock.move.line", "state", "done", user_id=self.warehouse.id)
        self.assert_blocked_unchanged(lambda: model.create(values))

    def test_completed_move_facts_and_state_cannot_be_changed(self):
        move = self.done().with_user(self.warehouse)
        for values in (
            {"quantity": 11}, {"product_uom_qty": 11}, {"state": "assigned"},
            {"location_id": self.empty_location.id}, {"sale_line_id": False},
            {"picking_id": False}, {"company_id": self.other_company.id},
            {"origin_returned_move_id": move.id}, {"to_refund": True},
        ):
            with self.subTest(values=values):
                self.assert_blocked_unchanged(lambda values=values: move.write(values))

    def test_done_move_line_create_cannot_append_additional_physical_facts(self):
        move = self.done()
        values = {
            "product_id": self.product.id, "product_uom_id": self.product.uom_id.id,
            "location_id": self.location.id, "location_dest_id": self.customer_location.id,
            "quantity": 1, "picked": True,
        }
        for relation in ({"move_id": move.id}, {"picking_id": move.picking_id.id}):
            with self.subTest(relation=relation):
                self.assert_blocked_unchanged(lambda relation=relation: self.env["stock.move.line"].with_user(
                    self.warehouse,
                ).create({**values, **relation}))

    def test_completed_move_line_physical_identity_is_immutable(self):
        move = self.done()
        line = move.move_line_ids.with_user(self.warehouse)
        package = self.env["stock.quant.package"].create({"name": "Different physical package"})
        product = self.new_product(name="Replacement bearing", default_code="PHYS-REPLACEMENT")
        lot = self.env["stock.lot"].with_user(self.warehouse).create({
            "name": "PHYS-DONE-LOT", "product_id": self.product.id, "company_id": self.company.id,
        })
        draft = self.move()
        for values in (
            {"quantity": 11}, {"product_id": product.id}, {"lot_id": lot.id},
            {"product_uom_id": self.env.ref("uom.product_uom_dozen").id},
            {"location_id": self.empty_location.id}, {"location_dest_id": self.location.id},
            {"owner_id": self.owner.id}, {"package_id": package.id}, {"result_package_id": package.id},
            {"company_id": self.other_company.id}, {"move_id": draft.id}, {"picking_id": False},
        ):
            with self.subTest(values=values):
                self.assert_blocked_unchanged(lambda values=values: line.write(values))

    def test_completed_move_line_unlink_fails_before_unreserving_stock(self):
        line = self.done().move_line_ids.with_user(self.warehouse)
        self.assert_blocked_unchanged(lambda: line.unlink())

    def test_completed_picking_identity_and_date_are_immutable(self):
        picking = self.done().picking_id.with_user(self.warehouse)
        for values in ({"location_id": self.empty_location.id}, {"date_done": False}, {"company_id": self.other_company.id}):
            with self.subTest(values=values):
                self.assert_blocked_unchanged(lambda values=values: picking.write(values))

    def test_done_line_cannot_be_reparented_out_of_controlled_scope(self):
        line = self.done().move_line_ids.with_user(self.warehouse)
        ordinary = self.move(company=self.other_company, source=self.other_location)
        self.assert_blocked_unchanged(lambda: line.write({"move_id": ordinary.id, "company_id": self.other_company.id}))

    def test_ordinary_done_line_cannot_be_reparented_into_controlled_scope(self):
        ordinary = self.move(company=self.other_company, source=self.other_location)
        ordinary._action_done()
        managed = self.move()
        self.assert_blocked_unchanged(lambda: ordinary.move_line_ids.write({
            "move_id": managed.id, "company_id": self.company.id,
            "location_id": self.location.id,
        }))

    def test_raw_quant_create_write_and_inventory_mode_spoof_are_rejected(self):
        model = self.env["stock.quant"].with_user(self.warehouse).with_company(self.company)
        quant = model.search([("product_id", "=", self.product.id), ("location_id", "=", self.location.id)], limit=1)
        for context in ({}, {"inventory_mode": True}, {"portfolio_quant_internal": True}):
            with self.subTest(context=context):
                self.assert_blocked_unchanged(lambda context=context: quant.with_context(**context).write({"quantity": 999}))
                self.assert_blocked_unchanged(lambda context=context: model.with_context(**context).create({
                    "product_id": self.product.id, "location_id": self.location.id, "quantity": 999,
                }))

    def test_quant_physical_identity_and_reserved_facts_cannot_be_forged(self):
        quant = self.env["stock.quant"].with_user(self.warehouse).search([
            ("product_id", "=", self.product.id), ("location_id", "=", self.location.id),
        ], limit=1)
        for values in ({"location_id": self.empty_location.id}, {"owner_id": self.owner.id}, {"reserved_quantity": 0}):
            with self.subTest(values=values):
                self.assert_blocked_unchanged(lambda values=values: quant.write(values))

    def test_quant_context_and_ir_defaults_cannot_supply_physical_quantity(self):
        model = self.env["stock.quant"].with_user(self.warehouse).with_company(self.company)
        values = {"product_id": self.product.id, "location_id": self.location.id}
        self.assert_blocked_unchanged(lambda: model.with_context(default_quantity=999).create(values))
        self.env["ir.default"].set("stock.quant", "quantity", 999, user_id=self.warehouse.id)
        self.assert_blocked_unchanged(lambda: model.create(values))

    def test_standalone_sales_line_reservation_cannot_inherit_physical_quant_defaults(self):
        self.assertFalse(self.sales.has_group("stock.group_stock_user"))
        for origin in ("context", "ir.default"):
            with self.subTest(origin=origin):
                location = self.env["stock.location"].create({
                    "name": f"Empty standalone default source {origin}", "usage": "internal",
                    "company_id": self.company.id, "location_id": self.warehouse_a.view_location_id.id,
                })
                model = self.env["stock.move.line"].with_user(self.sales).with_company(self.company)
                if origin == "context":
                    model = model.with_context(default_quantity=999)
                else:
                    self.env["ir.default"].set("stock.quant", "quantity", 999, user_id=self.sales.id)

                def verify(line):
                    self.assertFalse(line.move_id)
                    self.assertFalse(line.picking_id)
                    quants = self.env["stock.quant"].search([
                        ("product_id", "=", self.product.id), ("location_id", "=", location.id),
                    ])
                    self.assertEqual(sum(quants.mapped("quantity")), 0)
                    self.assertEqual(sum(quants.mapped("reserved_quantity")), 1)

                self.assert_safe_native_defaults(lambda: model.create({
                    "company_id": self.company.id, "product_id": self.product.id,
                    "product_uom_id": self.product.uom_id.id, "location_id": location.id,
                    "location_dest_id": self.customer_location.id, "quantity": 1,
                }), verify)

    def test_native_receipt_quant_does_not_inherit_unrequested_reserved_defaults(self):
        for origin in ("context", "ir.default"):
            with self.subTest(origin=origin):
                location = self.env["stock.location"].create({
                    "name": f"Empty receipt default destination {origin}", "usage": "internal",
                    "company_id": self.company.id, "location_id": self.warehouse_a.view_location_id.id,
                })
                picking = self.env["stock.picking"].with_user(self.warehouse).with_company(self.company).create({
                    "company_id": self.company.id, "picking_type_id": self.warehouse_a.in_type_id.id,
                    "location_id": self.supplier_location.id, "location_dest_id": location.id,
                    "move_ids": [Command.create({
                        "name": "Receipt with bounded native quantity", "company_id": self.company.id,
                        "product_id": self.product.id, "product_uom": self.product.uom_id.id,
                        "product_uom_qty": 1, "location_id": self.supplier_location.id,
                        "location_dest_id": location.id,
                    })],
                })
                picking.move_ids.write({"quantity": 1, "picked": True})
                if origin == "context":
                    picking = picking.with_context(default_reserved_quantity=999)
                else:
                    self.env["ir.default"].set("stock.quant", "reserved_quantity", 999, user_id=self.warehouse.id)

                def verify(result):
                    self.assertEqual(picking.state, "done")
                    quants = self.env["stock.quant"].search([
                        ("product_id", "=", self.product.id), ("location_id", "=", location.id),
                    ])
                    self.assertEqual(sum(quants.mapped("quantity")), 1)
                    self.assertEqual(sum(quants.mapped("reserved_quantity")), 0)

                self.assert_safe_native_defaults(lambda: picking.button_validate(), verify)

    def test_quant_scope_uses_location_company_despite_forged_company_values(self):
        self.assert_blocked_unchanged(lambda: self.env["stock.quant"].with_user(self.warehouse).sudo().create({
            "product_id": self.product.id, "location_id": self.location.id,
            "company_id": self.other_company.id, "quantity": 100,
        }))

    def test_quant_scope_uses_candidate_location_when_entering_controlled_company(self):
        quant = self.env["stock.quant"].with_user(self.warehouse).search([
            ("product_id", "=", self.product.id), ("location_id", "=", self.other_location.id),
        ], limit=1)
        self.assert_blocked_unchanged(lambda: quant.sudo().write({"location_id": self.location.id}))

    def test_sales_cannot_apply_inventory_under_sudo(self):
        quant = self.env["stock.quant"].search([
            ("product_id", "=", self.product.id), ("location_id", "=", self.location.id),
        ], limit=1)
        quant.with_context(inventory_mode=True).write({"inventory_quantity": 99})
        self.assert_blocked_unchanged(lambda: quant.with_user(self.sales).sudo().action_apply_inventory(), AccessError)

    def test_confirmed_sale_commercial_content_is_locked_with_price_policy_off(self):
        for user in (self.sales, self.warehouse, self.finance):
            for values in ({"partner_id": self.vendor.id}, {"note": "Edited confirmed source"}):
                with self.subTest(actor=user.login, values=values), self.assertRaises(UserError):
                    self.source.with_user(user).write(values)
        with self.assertRaises(UserError):
            self.source.order_line.write({"product_uom_qty": 101})
        with self.assertRaises(UserError):
            self.env["sale.order.line"].with_user(self.sales).create({
                "order_id": self.source.id, "product_id": self.product.id, "product_uom_qty": 1,
            })

    def test_only_actual_responsible_sales_actor_can_confirm_enabled_sale(self):
        for user in (self.warehouse, self.finance, self.buyer, self.reviewer):
            order = self.make_sale()
            with self.subTest(actor=user.login), self.assertRaises(AccessError), self.cr.savepoint():
                order.with_user(user).sudo().action_confirm()
            self.assertEqual(order.state, "draft")

    def test_executed_sale_cannot_reopen_after_cancellation_with_price_policy_off(self):
        self.done()
        self.source.with_context(disable_cancel_warning=True).action_cancel()
        with self.assertRaises(UserError):
            self.source.action_draft()
        with self.assertRaises(UserError):
            self.source.write({"state": "draft"})

    def test_done_sales_line_move_without_procurement_group_prevents_source_reopening(self):
        source = self.make_sale(quantity=1)
        source.action_confirm()
        picking = self.env["stock.picking"].with_user(self.warehouse).with_company(self.company).create({
            "company_id": self.company.id, "picking_type_id": self.warehouse_a.out_type_id.id,
            "location_id": self.location.id, "location_dest_id": self.customer_location.id,
            "group_id": False,
            "move_ids": [Command.create({
                "name": "Manual delivery with real sales-line provenance", "company_id": self.company.id,
                "product_id": self.product.id, "product_uom": self.product.uom_id.id,
                "product_uom_qty": 1, "location_id": self.location.id,
                "location_dest_id": self.customer_location.id,
                "sale_line_id": source.order_line.id, "group_id": False,
            })],
        })
        self.validate(picking, 1)
        self.assertFalse(picking.group_id)
        self.assertFalse(picking.sale_id)
        self.assertNotIn(picking, source.picking_ids)
        self.assertEqual(source.order_line.move_ids.filtered(lambda move: move.state == "done"), picking.move_ids)
        source.with_context(disable_cancel_warning=True).action_cancel()
        self.assertEqual(source.state, "cancel")
        self.assertFalse(source.picking_ids.filtered(lambda item: item.state == "done"))
        self.assert_blocked_unchanged(lambda: source.action_draft())
        self.assertEqual(source.state, "cancel")

    def test_canceled_executed_sale_cannot_delete_its_completed_stock_source(self):
        source = self.make_sale(quantity=1)
        source.action_confirm()
        line = source.order_line
        picking = source.picking_ids
        self.validate(picking, 1)
        moves = picking.move_ids
        source.with_context(disable_cancel_warning=True).action_cancel()
        self.assertEqual(source.state, "cancel")
        self.assertFalse(source.sudo().portfolio_purchase_ids)
        self.assertFalse(source.sudo().portfolio_approval_history_ids)
        self.assertEqual(moves.state, "done")
        self.assertEqual(moves.sale_line_id, line)
        manager_source = source.with_user(self.reviewer)
        manager_source.check_access("unlink")
        self.assert_blocked_unchanged(lambda: manager_source.unlink())
        self.assertTrue(source.exists())
        self.assertTrue(line.exists())
        self.assertEqual(moves.sale_line_id, line)
        self.assertEqual(moves.state, "done")

    def test_manager_can_delete_an_unexecuted_canceled_sale(self):
        source = self.make_sale(quantity=1)
        source.action_confirm()
        line = source.order_line
        source.with_context(disable_cancel_warning=True).action_cancel()
        manager_source = source.with_user(self.reviewer)
        manager_source.check_access("unlink")
        manager_source.unlink()
        self.assertFalse(source.exists())
        self.assertFalse(line.exists())
        self.assertEqual(self.stock(), 40)

    def test_physical_delivered_quantity_and_method_are_not_public_inputs(self):
        line = self.source.order_line
        for values in ({"qty_delivered": 100}, {"qty_delivered_method": "manual"}):
            with self.subTest(values=values), self.assertRaises(UserError):
                line.write(values)
        self.assertEqual(line.qty_delivered, 0)
        self.assertEqual(line.qty_delivered_method, "stock_move")

    def test_native_return_creation_keeps_draft_origin_and_refund_semantics(self):
        picking = self.done().picking_id.with_user(self.warehouse)
        wizard = self.env["stock.return.picking"].with_user(self.warehouse).with_context(
            active_model="stock.picking", active_id=picking.id, active_ids=picking.ids,
        ).create({})
        wizard.product_return_moves.write({"quantity": 1})
        result = wizard.action_create_returns()
        returned = self.env["stock.picking"].browse(result["res_id"])
        self.assertTrue(returned.move_ids.origin_returned_move_id)
        self.assertNotEqual(returned.state, "done")
        returned.move_ids.write({"to_refund": True})
        self.assertTrue(returned.move_ids.to_refund)
