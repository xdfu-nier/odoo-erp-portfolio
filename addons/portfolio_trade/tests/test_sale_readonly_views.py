"""Source sales views follow the actual actor's Sales role, including in sudo."""
from lxml import etree
from odoo import Command
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install", "portfolio_sale_readonly_views")
class TestSaleReadOnlyViews(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        roles = {
            "billing": "account.group_account_invoice",
            "buyer": "purchase.group_purchase_user",
            "sales": "sales_team.group_sale_salesman",
            "manager": "sales_team.group_sale_manager",
            "sales_billing": "sales_team.group_sale_salesman,account.group_account_invoice",
        }
        for role, groups in roles.items():
            setattr(cls, role, new_test_user(
                cls.env, login=f"readonly_view_{role}", groups=groups,
                company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
            ))
        cls.view_ids = {
            "form": cls.env.ref("sale.view_order_form").id,
            "list": cls.env.ref("sale.view_order_tree").id,
        }
        cls.sales_write_buttons = {
            "payment_action_capture", "payment_action_void", "action_quotation_send",
            "action_confirm", "action_cancel", "action_draft", "action_lock", "action_unlock",
            "action_portfolio_submit", "action_portfolio_approve", "action_portfolio_reject_wizard",
            str(cls.env.ref("sale.action_view_sale_advance_payment_inv").id),
        }

    def sale_model(self, actor):
        return self.env["sale.order"].with_user(actor).with_context(
            allowed_company_ids=self.company.ids,
        )

    def view_arches(self, actor):
        result = self.sale_model(actor).get_views([
            (view_id, view_type) for view_type, view_id in self.view_ids.items()
        ])
        self.assertIn("sale.order", result["models"])
        for view_type, view in result["views"].items():
            self.assertEqual(view["model"], "sale.order")
            self.assertEqual(view["id"], self.view_ids[view_type])
        return {
            view_type: etree.fromstring(view["arch"].encode())
            for view_type, view in result["views"].items()
        }

    def assert_readonly(self, arches, billing=False):
        for view_type, arch in arches.items():
            for attribute in ("create", "edit", "delete"):
                with self.subTest(view_type=view_type, attribute=attribute):
                    self.assertIn(arch.get(attribute), ("0", "false", "False"))
            buttons = {button.get("name") for button in arch.xpath("./header//button")}
            self.assertFalse(buttons & self.sales_write_buttons, buttons & self.sales_write_buttons)
        form = arches["form"]
        self.assertTrue(form.xpath("./header//button[@name='action_portfolio_trace']"))
        self.assertEqual(bool(form.xpath(
            "./header//button[@name='action_portfolio_create_invoice']",
        )), billing)
        self.assertTrue(form.xpath(".//button[@name='action_view_invoice']"))

    def assert_writer(self, arches, actor):
        for view_type, arch in arches.items():
            for attribute in ("create", "edit"):
                with self.subTest(view_type=view_type, attribute=attribute):
                    self.assertNotIn(arch.get(attribute), ("0", "false", "False"))
            self.assertEqual(
                arch.get("delete") not in ("0", "false", "False"),
                self.sale_model(actor).has_access("unlink"),
            )
        form_buttons = {button.get("name") for button in arches["form"].xpath("./header//button")}
        self.assertTrue({
            "action_confirm", "action_quotation_send", "action_cancel", "action_draft",
            "action_portfolio_submit", "action_portfolio_trace",
        } <= form_buttons)
        self.assertTrue(arches["list"].xpath("./header//button"))

    def test_billing_source_views_are_readonly_with_finance_and_trace_actions(self):
        self.assertFalse(self.billing.has_group("sales_team.group_sale_salesman"))
        self.assert_readonly(self.view_arches(self.billing), billing=True)

    def test_buyer_source_views_are_readonly_with_trace_only(self):
        self.assertFalse(self.buyer.has_group("sales_team.group_sale_salesman"))
        self.assert_readonly(self.view_arches(self.buyer))
        self.assertTrue(self.view_arches(self.buyer)["form"].xpath(
            ".//button[@name='action_portfolio_purchases']",
        ))

    def test_sales_writers_and_actual_billing_sudo_keep_isolated_view_arches(self):
        for actor in (self.sales, self.manager, self.sales_billing):
            with self.subTest(actor=actor.login):
                self.assertTrue(actor.has_group("sales_team.group_sale_salesman"))
                first = self.view_arches(actor)
                self.assert_writer(first, actor)
                if actor == self.manager:
                    self.assertTrue(first["form"].xpath(
                        "./header//button[@name='action_portfolio_approve']",
                    ))
                if actor == self.sales_billing:
                    self.assertTrue(first["form"].xpath(
                        "./header//button[@name='action_portfolio_create_invoice']",
                    ))
                self.assert_readonly(self.view_arches(self.billing), billing=True)
                elevated = {
                    view_type: etree.fromstring(self.sale_model(self.billing).sudo().get_view(
                        view_id=view_id, view_type=view_type,
                    )["arch"].encode())
                    for view_type, view_id in self.view_ids.items()
                }
                self.assert_readonly(elevated, billing=True)
                self.assert_readonly(self.view_arches(self.buyer))
                self.assert_writer(self.view_arches(actor), actor)
                self.assert_writer(first, actor)
                self.assert_readonly(self.view_arches(self.billing), billing=True)
