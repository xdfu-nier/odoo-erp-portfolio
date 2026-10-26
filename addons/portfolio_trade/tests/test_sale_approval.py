from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestSaleApproval(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env["res.company"].create({"name": "Approval isolated company"})
        cls.sales = new_test_user(
            cls.env, login="approval_sales", groups="sales_team.group_sale_salesman",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.manager = new_test_user(
            cls.env, login="approval_manager", groups="sales_team.group_sale_manager",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.manager2 = new_test_user(
            cls.env, login="approval_manager2", groups="sales_team.group_sale_manager",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.partner = cls.env["res.partner"].create({"name": "Approval customer"})
        cls.product = cls.env["product.product"].create({
            "name": "Approval bearing", "type": "service", "list_price": 100,
            "taxes_id": [Command.clear()],
        })
        cls.company.write({"portfolio_approval_enabled": True})

    def order(self, price=50000, discount=0, user=None, **extra):
        user = user or self.sales
        return self.env["sale.order"].with_user(user).with_company(self.company).create({
            "partner_id": self.partner.id, "user_id": user.id,
            "order_line": [Command.create({
                "product_id": self.product.id, "product_uom_qty": 1,
                "price_unit": price, "discount": discount, "tax_id": [Command.clear()],
            })], **extra,
        })

    def approve(self, order):
        order.action_portfolio_submit()
        order.with_user(self.manager).action_portfolio_approve()

    def test_01_standard_confirmation_requires_approval(self):
        order = self.order()
        with self.assertRaises(UserError):
            order.action_confirm()
        self.approve(order)
        order.action_confirm()
        self.assertEqual(order.state, "sale")

    def test_amount_and_discount_boundaries(self):
        for price, discount, required in [
            (49999.99, 0, False), (50000, 0, True),
            (100, 10, False), (100, 15, True),
        ]:
            with self.subTest(price=price, discount=discount):
                order = self.order(price, discount)
                if required:
                    with self.assertRaises(UserError):
                        order.action_confirm()
                else:
                    order.action_confirm()
                    self.assertEqual(order.state, "sale")

    def test_sales_cannot_approve_or_configure_company(self):
        order = self.order()
        order.action_portfolio_submit()
        with self.assertRaises(AccessError):
            order.action_portfolio_approve()
        for user in (self.sales, self.manager):
            with self.assertRaises(AccessError):
                self.company.with_user(user).write({"portfolio_approval_enabled": False})

    def test_creator_responsible_and_submitter_cannot_self_approve(self):
        created = self.order(user=self.manager, user_id=self.sales.id)
        created.with_user(self.sales).action_portfolio_submit()
        with self.assertRaises(UserError):
            created.with_user(self.manager).action_portfolio_approve()
        responsible = self.order(user=self.manager2, user_id=self.manager.id)
        responsible.with_user(self.manager2).action_portfolio_submit()
        with self.assertRaises(UserError):
            responsible.with_user(self.manager).action_portfolio_approve()
        submitted = self.order()
        submitted.with_user(self.manager).action_portfolio_submit()
        with self.assertRaises(UserError):
            submitted.with_user(self.manager).action_portfolio_approve()

    def test_forged_fields_and_context_cannot_authorize(self):
        order = self.order()
        for values in (
            {"portfolio_approval_state": "approved"},
            {"create_uid": self.manager.id},
            {"portfolio_creator_id": self.manager.id},
            {"portfolio_approval_enabled": False},
            {"state": "sale"},
        ):
            with self.subTest(values=values), self.assertRaises(UserError):
                order.with_context(portfolio_internal=True, skip_approval=True).write(values)
        for values in ({"portfolio_approval_state": "approved"}, {"create_uid": self.manager.id}, {"state": "sale"}):
            with self.assertRaises(UserError):
                self.order(**values)
        for key, value in (("portfolio_approval_state", "approved"), ("create_uid", self.manager.id)):
            with self.assertRaises(AccessError):
                self.env["sale.order"].with_user(self.sales).with_context(**{f"default_{key}": value}).create({
                    "partner_id": self.partner.id,
                })

    def test_company_and_currency_edits_invalidate(self):
        self.manager.write({"company_ids": [Command.set((self.company | self.other_company).ids)]})
        self.other_company.write({"portfolio_approval_enabled": True})
        order = self.order()
        self.approve(order)
        audit_company = order.portfolio_approval_history_ids.company_id
        manager_order = order.with_user(self.manager).with_context(
            allowed_company_ids=(self.company | self.other_company).ids,
        )
        manager_order.write({"company_id": self.other_company.id, "warehouse_id": False})
        self.assertEqual(manager_order.portfolio_approval_state, "draft")
        self.assertEqual(manager_order.portfolio_approval_history_ids.company_id, audit_company)
        foreign = self.env["res.currency"].with_context(active_test=False).search([
            ("id", "!=", self.company.currency_id.id),
        ], limit=1)
        foreign.active = True
        order = self.order()
        self.approve(order)
        order.write({"currency_id": foreign.id})
        self.assertEqual(order.portfolio_approval_state, "draft")
        with self.assertRaises(UserError):
            order.action_confirm()

    def test_notes_do_not_trigger_product_discount_policy(self):
        order = self.order(price=100)
        order.write({"order_line": [Command.create({
            "display_type": "line_note", "name": "Text only", "discount": 50,
        })]})
        order.action_confirm()
        self.assertEqual(order.state, "sale")

    def test_old_submission_cannot_be_approved_after_policy_change(self):
        order = self.order()
        order.action_portfolio_submit()
        self.assertTrue(order.portfolio_approval_current)
        self.company.write({"portfolio_approval_discount": 5})
        self.assertFalse(order.portfolio_approval_current)
        with self.assertRaises(UserError):
            order.with_user(self.manager).action_portfolio_approve()
        order.action_portfolio_submit()
        order.with_user(self.manager).action_portfolio_approve()

    def test_history_is_immutable_and_rejection_requires_reason(self):
        order = self.order()
        order.action_portfolio_submit()
        with self.assertRaises(UserError):
            order.with_user(self.manager).action_portfolio_reject(" ")
        order.with_user(self.manager).action_portfolio_reject("Discount needs revision")
        self.assertEqual(order.portfolio_approval_state, "rejected")
        history = order.portfolio_approval_history_ids
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1].reason, "Discount needs revision")
        for user in (self.sales, self.manager):
            with self.assertRaises(AccessError):
                history.with_user(user).write({"reason": "forged"})
            with self.assertRaises(AccessError):
                history.with_user(user).unlink()
            with self.assertRaises(AccessError):
                self.env["portfolio.sale.approval"].with_user(user).create({
                    "order_id": order.id, "event": "approved", "reason": "forged",
                })
        order.order_line.write({"discount": 15})
        self.approve(order)
        self.assertEqual(order.portfolio_approval_state, "approved")
        self.assertEqual(len(order.portfolio_approval_history_ids), 5)

    def test_order_commercial_edits_invalidate(self):
        partner = self.partner.copy({"name": "Second customer"})
        term = self.env["account.payment.term"].create({"name": "Approval terms"})
        for values in (
            {"partner_id": partner.id}, {"partner_invoice_id": partner.id},
            {"partner_shipping_id": partner.id}, {"payment_term_id": term.id},
            {"commitment_date": "2027-01-01 10:00:00"}, {"user_id": self.manager2.id},
        ):
            with self.subTest(values=values):
                order = self.order()
                self.approve(order)
                order.with_user(self.manager).write(values)
                self.assertEqual(order.with_user(self.manager).portfolio_approval_state, "draft")
                with self.assertRaises(UserError):
                    order.action_confirm()

    def test_line_commercial_edits_invalidate(self):
        tax = self.env["account.tax"].create({"name": "Approval tax", "amount": 5, "company_id": self.company.id})
        dozen = self.env.ref("uom.product_uom_dozen")
        for values in (
            {"product_id": self.product.copy().id}, {"product_uom": dozen.id},
            {"product_uom_qty": 2}, {"price_unit": 60000}, {"discount": 15},
            {"tax_id": [Command.set(tax.ids)]},
        ):
            with self.subTest(values=values):
                order = self.order()
                self.approve(order)
                order.order_line.write(values)
                self.assertEqual(order.portfolio_approval_state, "draft")

    def test_line_add_delete_and_move_invalidate_both_parents(self):
        source, target = self.order(), self.order()
        self.approve(source)
        self.approve(target)
        source.order_line.write({"order_id": target.id})
        self.assertEqual(source.portfolio_approval_state, "draft")
        self.assertEqual(target.portfolio_approval_state, "draft")
        self.approve(target)
        target.order_line[:1].unlink()
        self.assertEqual(target.portfolio_approval_state, "draft")
        self.approve(target)
        target.write({"order_line": [Command.create({
            "product_id": self.product.id, "price_unit": 100, "product_uom_qty": 1,
        })]})
        self.assertEqual(target.portfolio_approval_state, "draft")

    def test_confirmed_commercial_changes_are_blocked(self):
        order = self.order()
        self.approve(order)
        order.action_confirm()
        for action in (
            lambda: order.write({"payment_term_id": False}),
            lambda: order.order_line.write({"product_uom_qty": 2}),
            lambda: order.order_line.unlink(),
            lambda: self.env["sale.order.line"].with_user(self.sales).create({
                "order_id": order.id, "product_id": self.product.id, "price_unit": 1,
            }),
        ):
            with self.assertRaises(UserError):
                action()

    def test_foreign_currency_is_explicitly_rejected(self):
        foreign = self.env["res.currency"].with_context(active_test=False).search([("id", "!=", self.company.currency_id.id)], limit=1)
        foreign.active = True
        pricelist = self.env["product.pricelist"].create({"name": "Foreign", "currency_id": foreign.id})
        order = self.order(pricelist_id=pricelist.id)
        with self.assertRaises(UserError):
            order.action_portfolio_submit()
        with self.assertRaises(UserError):
            order.action_confirm()

    def test_policy_change_invalidates_previous_approval(self):
        order = self.order()
        self.approve(order)
        version = self.company.portfolio_approval_policy_version
        self.company.write({"portfolio_approval_amount": 40000})
        self.assertGreater(self.company.portfolio_approval_policy_version, version)
        with self.assertRaises(UserError):
            order.action_confirm()
        order.action_portfolio_submit()
        order.with_user(self.manager).action_portfolio_approve()
        order.action_confirm()

    def test_policy_validation(self):
        for values in (
            {"portfolio_approval_amount": -1}, {"portfolio_approval_amount": float("inf")},
            {"portfolio_approval_amount": float("nan")},
            {"portfolio_approval_discount": -1}, {"portfolio_approval_discount": 101},
        ):
            with self.cr.savepoint(), self.assertRaises(ValidationError):
                self.company.write(values)

    def test_cross_company_access_and_disabled_compatibility(self):
        other = self.env["sale.order"].with_company(self.other_company).create({
            "partner_id": self.partner.id, "company_id": self.other_company.id,
            "order_line": [Command.create({"product_id": self.product.id, "price_unit": 50000})],
        })
        other.action_confirm()
        self.assertEqual(other.state, "sale")
        for action in (
            lambda: other.with_user(self.manager).read(["partner_id"]),
            lambda: other.with_user(self.manager).write({"partner_id": self.partner.id}),
            lambda: other.with_user(self.manager).action_portfolio_submit(),
        ):
            with self.assertRaises(AccessError):
                action()
        own = self.order()
        own.action_portfolio_submit()
        event = own.portfolio_approval_history_ids
        self.sales.write({"company_ids": [Command.set(self.other_company.ids)], "company_id": self.other_company.id})
        with self.assertRaises(AccessError):
            event.with_user(self.sales).with_context(allowed_company_ids=self.other_company.ids).read()
