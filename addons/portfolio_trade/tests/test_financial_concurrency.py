"""Real repeatable-read contenders for invoice, credit and return allowances.

Only the dedicated acceptance database receives committed fixtures. Every race
uses ordinary business actors, an old snapshot, a third-connection lock witness,
and a new cursor after PostgreSQL rejects the stale transaction.
"""
import logging
import sys
import threading
import time
import traceback
import uuid

from odoo import SUPERUSER_ID, Command, api, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from psycopg2.errors import SerializationFailure

from ..models.financial_policy import financial_facts, physical_facts

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install", "portfolio_financial_concurrency")
class TestFinancialConcurrency(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls.cr.dbname != "odoo_portfolio_m1_test":
            raise AssertionError("Committed concurrency fixtures require odoo_portfolio_m1_test")
        with cls.registry.cursor() as cr:
            # Activating a second currency normally enables pricelists for
            # every company. Keep this committed fixture's setup local using
            # the native company-pricelist creation switch, and restore the
            # currency feature groups before creating any fixture actors.
            env = api.Environment(cr, SUPERUSER_ID, {"disable_company_pricelist_creation": True})
            employee_group = env.ref("base.group_user")
            feature_groups = env.ref("base.group_multi_currency") | env.ref("product.group_product_pricelist")
            features_before = {
                group.id: (group in employee_group.implied_ids,
                           sorted(group.with_context(active_test=False).users.ids))
                for group in feature_groups
            }
            pricelists_before = env["product.pricelist"].with_context(active_test=False).search([]).ids
            cny = env.ref("base.CNY")
            cny.active = True
            company = env["res.company"].create({
                "name": f"Financial RR race {uuid.uuid4().hex[:8]}", "currency_id": cny.id,
            })
            env.user.write({"company_ids": [Command.link(company.id)]})
            env = env(context={
                "allowed_company_ids": company.ids, "tracking_disable": True,
                "disable_company_pricelist_creation": True,
            })
            # The native generic chart chooses USD. Restore the explicit CNY
            # before enabling fulfillment, and never load a developer's chart.
            env["account.chart.template"].try_loading("generic_coa", company=company, install_demo=False)
            company.currency_id = cny
            if not company.account_fiscal_country_id:
                company.account_fiscal_country_id = env.ref("base.us")
            for group in feature_groups:
                was_implied, members = features_before[group.id]
                if was_implied:
                    employee_group._apply_group(group)
                else:
                    employee_group._remove_group(group)
                if sorted(group.with_context(active_test=False).users.ids) != members:
                    group.with_context(active_test=False).write({"users": [Command.set(members)]})
                if (group in employee_group.implied_ids,
                        sorted(group.with_context(active_test=False).users.ids)) != features_before[group.id]:
                    raise AssertionError("Native chart setup must preserve preexisting currency feature groups")
            if env["product.pricelist"].with_context(active_test=False).search([]).ids != pricelists_before:
                raise AssertionError("Native chart setup must not create or change global company pricelist defaults")
            env = env(context={"allowed_company_ids": company.ids, "tracking_disable": True})
            pricelist = env["product.pricelist"].create({
                "name": f"Financial RR CNY {uuid.uuid4().hex[:8]}",
                "company_id": company.id, "currency_id": cny.id,
            })
            cls.pricelist_id = pricelist.id
            _logger.info("Financial RR chart preserved preexisting currency feature groups and pricelist defaults")
            company.portfolio_fulfillment_enabled = True
            if company.currency_id != cny:
                raise AssertionError("The isolated native chart company must use explicit CNY")
            accounts = env["account.account"].with_company(company)
            account_domain = accounts._check_company_domain(company)
            revenue = accounts.search([
                *account_domain, ("account_type", "=", "income"), ("deprecated", "=", False),
                ("id", "!=", company.account_journal_early_pay_discount_gain_account_id.id),
            ], limit=1)
            expense = accounts.search([
                *account_domain, ("account_type", "=", "expense"), ("deprecated", "=", False),
            ], limit=1)
            journal = env["account.journal"].search([
                ("company_id", "=", company.id), ("type", "=", "sale"),
            ], limit=1)
            if not revenue or not expense or not journal:
                raise AssertionError("The isolated native chart must provide real accounts and a sales journal")
            cls.company_id, cls.currency_id = company.id, cny.id
            cls.revenue_id, cls.expense_id, cls.journal_id = revenue.id, expense.id, journal.id
            cls.actor_ids = []
            for role, groups in (
                ("sales", "sales_team.group_sale_salesman"),
                ("warehouse", "stock.group_stock_user"),
                ("inventory", "stock.group_stock_manager"),
                ("billing", "account.group_account_invoice"),
            ):
                actor = new_test_user(
                    env, login=f"financial_rr_{role}_{uuid.uuid4().hex}", groups=groups,
                    company_id=company.id, company_ids=[Command.set(company.ids)],
                )
                setattr(cls, f"{role}_id", actor.id)
                cls.actor_ids.append(actor.id)
            warehouse = env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
            cls.location_id = warehouse.lot_stock_id.id
            customer = env["res.partner"].create({
                "name": f"Financial RR customer {uuid.uuid4().hex[:8]}",
                "ref": f"RR-C-{uuid.uuid4().hex[:8]}",
            })
            cls.customer_id = customer.id
            cr.commit()
        cls.addClassCleanup(cls._archive_actors)

    @classmethod
    def _archive_actors(cls):
        with cls.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {"allowed_company_ids": [cls.company_id]})
            env["res.users"].browse(cls.actor_ids).write({"active": False})
            cr.commit()

    def environment(self, cr, uid):
        # A worker must use the runner's loaded registry without taking the
        # main thread's registry lock. Each retry gets a separate cursor/tx.
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, uid, {
            "allowed_company_ids": [self.company_id], "tracking_disable": True, "lang": "en_US",
            "mail_create_nosubscribe": True,
        })

    def assert_actor(self, env, role):
        self.assertEqual(env.uid, getattr(self, f"{role}_id"))
        self.assertFalse(env.su)
        self.assertEqual(env.user.company_ids.ids, [self.company_id])
        self.assertEqual(env.companies.ids, [self.company_id])
        if role == "billing":
            self.assertTrue(env.user.has_group("account.group_account_invoice"))
            for group in ("sales_team.group_sale_salesman", "stock.group_stock_user", "base.group_system"):
                self.assertFalse(env.user.has_group(group))
        elif role == "warehouse":
            self.assertTrue(env.user.has_group("stock.group_stock_user"))
            for group in ("sales_team.group_sale_salesman", "stock.group_stock_manager", "base.group_system"):
                self.assertFalse(env.user.has_group(group))

    def invoice(self, env, fixture, quantity):
        """Native semantic account ORM supports a partial claim on this SO line.

        The public SO invoice action intentionally takes all remaining units;
        partial contenders therefore use the supported ordinary Billing ORM.
        """
        self.assert_actor(env, "billing")
        return env["account.move"].create({
            "move_type": "out_invoice", "company_id": self.company_id,
            "currency_id": self.currency_id, "partner_id": self.customer_id,
            "invoice_date": fields.Date.today(), "journal_id": self.journal_id,
            "invoice_line_ids": [Command.create({
                "product_id": fixture["product_id"], "product_uom_id": fixture["uom_id"],
                "account_id": self.revenue_id, "quantity": quantity, "price_unit": 100,
                "discount": 15, "tax_ids": [Command.clear()],
                "sale_line_ids": [Command.set([fixture["line_id"]])],
            })],
        })

    def return_wizard(self, env, fixture, quantity):
        self.assert_actor(env, "warehouse")
        wizard = env["stock.return.picking"].with_context(
            active_model="stock.picking", active_id=fixture["delivery_id"],
            active_ids=[fixture["delivery_id"]],
        ).create({})
        self.assertEqual(wizard.product_return_moves.move_id.ids, [fixture["origin_id"]])
        wizard.product_return_moves.write({"quantity": quantity, "to_refund": True})
        return wizard

    def assert_return_identity(self, picking, fixture, quantity):
        moves = picking.move_ids
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves.origin_returned_move_id.ids, [fixture["origin_id"]])
        self.assertEqual(moves.sale_line_id.ids, [fixture["line_id"]])
        self.assertEqual(moves.product_id.ids, [fixture["product_id"]])
        self.assertEqual(moves.product_uom.ids, [fixture["uom_id"]])
        self.assertEqual(moves.company_id.ids, [self.company_id])
        self.assertEqual(moves.product_uom_qty, quantity)
        self.assertTrue(moves.to_refund)
        self.assertEqual(moves.location_id, moves.origin_returned_move_id.location_dest_id)
        self.assertEqual(moves.location_dest_id.id, self.location_id)
        for detail in moves.move_line_ids:
            self.assertEqual(detail.move_id, moves)
            self.assertEqual(detail.picking_id, picking)
            self.assertEqual(detail.product_id, moves.product_id)
            self.assertEqual(detail.company_id, moves.company_id)
            self.assertEqual(detail.product_uom_id, moves.product_uom)
            self.assertEqual(detail.location_id, moves.location_id)
            self.assertEqual(detail.location_dest_id, moves.location_dest_id)

    def complete(self, env, picking):
        self.assert_actor(env, "warehouse")
        result = picking.button_validate()
        self.assertFalse(isinstance(result, dict), f"Exact native quantities should complete directly: {result}")
        self.assertEqual(picking.state, "done")
        self.assertEqual(picking.write_uid.id, self.warehouse_id)

    def make_fixture(self, kind):
        with self.registry.cursor() as cr:
            admin = self.environment(cr, SUPERUSER_ID)
            product = admin["product.product"].create({
                "name": f"Financial RR {kind} {uuid.uuid4().hex[:8]}",
                "default_code": f"RR-{uuid.uuid4().hex[:12]}",
                "type": "consu", "is_storable": True, "invoice_policy": "delivery",
                "list_price": 100, "standard_price": 60,
                "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
                "property_account_income_id": self.revenue_id,
                "property_account_expense_id": self.expense_id,
            })
            inventory = self.environment(cr, self.inventory_id)
            quant = inventory["stock.quant"].with_context(inventory_mode=True).create({
                "product_id": product.id, "location_id": self.location_id, "inventory_quantity": 70,
            })
            quant.action_apply_inventory()
            sales = self.environment(cr, self.sales_id)
            source = sales["sale.order"].create({
                "partner_id": self.customer_id, "company_id": self.company_id, "user_id": self.sales_id,
                "pricelist_id": self.pricelist_id,
                "order_line": [Command.create({
                    "product_id": product.id, "product_uom": product.uom_id.id,
                    "product_uom_qty": 70, "price_unit": 100, "discount": 15,
                    "tax_id": [Command.clear()],
                })],
            })
            source.action_confirm()
            warehouse = self.environment(cr, self.warehouse_id)
            delivery = warehouse["stock.picking"].browse(source.picking_ids.ids)
            self.assertEqual(len(delivery.move_ids), 1)
            delivery.move_ids.write({"quantity": 70, "picked": True})
            self.complete(warehouse, delivery)
            fixture = {
                "kind": kind, "product_id": product.id, "uom_id": product.uom_id.id,
                "order_id": source.id, "line_id": source.order_line.id,
                "delivery_id": delivery.id, "origin_id": delivery.move_ids.id,
            }
            billing = self.environment(cr, self.billing_id)
            if kind == "credit":
                originals = self.invoice(billing, fixture, 40) | self.invoice(billing, fixture, 30)
                originals.action_post()
                self.assertEqual(originals.mapped("state"), ["posted", "posted"])
                fixture["original_ids"] = originals.ids
                fixture["original_line_ids"] = originals.invoice_line_ids.ids
                self.assertEqual(len(fixture["original_line_ids"]), 2)
                returned = warehouse["stock.picking"].browse(
                    self.return_wizard(warehouse, fixture, 10).action_create_returns()["res_id"],
                )
                returned.move_ids.write({"quantity": 10, "picked": True})
                self.complete(warehouse, returned)
                self.assert_return_identity(returned, fixture, 10)
                wizard_ids = []
                for original, original_line_id in zip(originals, fixture["original_line_ids"]):
                    action = original.action_portfolio_partial_credit()
                    self.assertEqual(action["context"]["default_original_line_id"], original_line_id)
                    wizard = billing["portfolio.partial.credit"].with_context(**action["context"]).create({
                        "original_line_id": original_line_id, "quantity": 7,
                    })
                    self.assertEqual(wizard.available_quantity, 10)
                    wizard_ids.append(wizard.id)
                fixture["wizard_ids"] = wizard_ids
            elif kind == "return":
                # Both native wizard inputs exist before either picking is
                # created, so planned siblings cannot reduce a default input.
                wizards = [self.return_wizard(warehouse, fixture, 40) for _ in range(2)]
                for wizard in wizards:
                    self.assertEqual(wizard.product_return_moves.quantity, 40)
                picking_ids = []
                for wizard in wizards:
                    picking = warehouse["stock.picking"].browse(wizard.action_create_returns()["res_id"])
                    picking.move_ids.write({"quantity": 40, "picked": True})
                    self.assertNotEqual(picking.state, "done")
                    self.assert_return_identity(picking, fixture, 40)
                    picking_ids.append(picking.id)
                fixture["return_ids"] = picking_ids
            source_line = billing["sale.order.line"].browse(fixture["line_id"])
            self.assertEqual(physical_facts(source_line)[source_line.id], {
                "delivered": 70, "returned": 10 if kind == "credit" else 0,
            })
            cr.commit()
        return fixture

    def snapshot(self, env, fixture, contender):
        """Materialize native source, AML, move/SML and quant facts before guard."""
        source = env["sale.order.line"].browse(fixture["line_id"])
        source.read(["order_id", "product_id", "product_uom", "product_uom_qty", "invoice_lines"])
        source.order_id.read(["company_id", "state"])
        moves = env["stock.move"].search([("sale_line_id", "=", source.id)], order="id")
        moves.read([
            "state", "quantity", "product_uom_qty", "company_id", "sale_line_id", "product_id",
            "product_uom", "origin_returned_move_id", "location_id", "location_dest_id", "move_line_ids",
        ])
        moves.move_line_ids.read([
            "state", "quantity", "product_id", "product_uom_id", "company_id", "move_id",
            "picking_id", "location_id", "location_dest_id", "lot_id", "package_id", "owner_id",
        ])
        quants = env["stock.quant"].search([("product_id", "=", fixture["product_id"])], order="id")
        quants.read(["quantity", "reserved_quantity", "location_id", "lot_id", "package_id", "owner_id"])
        result = {
            "financial": financial_facts(source)[source.id],
            "physical": physical_facts(source)[source.id],
            "stock": sum(quants.filtered(lambda quant: quant.location_id.id == self.location_id).mapped("quantity")),
            "moves": [(move.id, move.state, move.quantity) for move in moves],
        }
        if fixture["kind"] != "return":
            entries = env["account.move.line"].search([
                ("sale_line_ids", "in", source.ids), ("display_type", "=", "product"),
            ], order="id")
            entries.read([
                "move_id", "quantity", "price_unit", "discount", "product_id", "product_uom_id",
                "company_id", "sale_line_ids", "balance", "amount_currency",
            ])
            entries.move_id.read(["state", "move_type", "reversed_entry_id"])
            result["account_lines"] = [(entry.id, entry.move_id.id, entry.quantity) for entry in entries]
        if fixture["kind"] == "credit":
            wizard = env["portfolio.partial.credit"].browse(fixture["wizard_ids"][contender])
            result["available_credit"] = wizard.available_quantity
            result["original_line_id"] = wizard.original_line_id.id
        env.cr.execute("SELECT pg_backend_pid(), txid_current(), current_setting('transaction_isolation')")
        result["pid"], result["txid"], result["isolation"] = env.cr.fetchone()
        self.assertEqual(result["isolation"], "repeatable read")
        return result

    def perform(self, env, fixture, contender):
        kind = fixture["kind"]
        if kind == "invoice":
            move = self.invoice(env, fixture, (40, 35)[contender])
        elif kind == "credit":
            self.assert_actor(env, "billing")
            wizard = env["portfolio.partial.credit"].browse(fixture["wizard_ids"][contender])
            self.assertEqual(wizard.original_line_id.id, fixture["original_line_ids"][contender])
            action = wizard.action_create_credit()
            self.assertEqual(action["res_model"], "account.move")
            move = env["account.move"].browse(action["res_id"])
            self.assertEqual(move.reversed_entry_id.id, fixture["original_ids"][contender])
        else:
            picking = env["stock.picking"].browse(fixture["return_ids"][contender])
            self.complete(env, picking)
            self.assert_return_identity(picking, fixture, 40)
            return picking.id
        self.assertEqual(move.create_uid.id, self.billing_id)
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.invoice_line_ids.sale_line_ids.ids, [fixture["line_id"]])
        self.assertEqual(move.invoice_line_ids.product_id.id, fixture["product_id"])
        self.assertEqual(move.invoice_line_ids.product_uom_id.id, fixture["uom_id"])
        self.assertEqual(move.invoice_line_ids.price_unit, 100)
        self.assertEqual(move.invoice_line_ids.discount, 15)
        self.assertEqual(sum(move.line_ids.mapped("balance")), 0)
        return move.id

    def bounded_transaction(self, cr):
        # Technical connection settings bound failure cleanup without changing
        # any business fact or replacing the native locking protocol.
        cr.execute("SELECT set_config('lock_timeout', '15000', true), set_config('statement_timeout', '20000', true)")

    def race(self, kind, first):
        fixture = self.make_fixture(kind)
        second = 1 - first
        actor = self.warehouse_id if kind == "return" else self.billing_id
        result = {"retries": 0, "attempts": [], "fixture": fixture, "winner": first}
        preloaded = threading.Event()
        enter_guard = threading.Event()
        stop = threading.Event()
        with self.registry.cursor() as first_cr:
            self.bounded_transaction(first_cr)
            first_env = self.environment(first_cr, actor)
            first_cr.execute("SELECT pg_backend_pid()")
            first_pid = first_cr.fetchone()[0]
            result["winning_id"] = self.perform(first_env, fixture, first)

            def worker():
                try:
                    for attempt in range(5):
                        if stop.is_set():
                            return
                        with self.registry.cursor() as second_cr:
                            self.bounded_transaction(second_cr)
                            env = self.environment(second_cr, actor)
                            old = self.snapshot(env, fixture, second)
                            result["attempts"].append(old)
                            if attempt == 0:
                                preloaded.set()
                                if not enter_guard.wait(10):
                                    raise AssertionError("The old-snapshot contender was not released")
                                if stop.is_set():
                                    return
                            try:
                                result["second_id"] = self.perform(env, fixture, second)
                                second_cr.commit()
                                result["success"] = True
                                return
                            except SerializationFailure:
                                second_cr.rollback()
                                result["retries"] += 1
                                _logger.info("Financial %s race serialization retry %s in a new cursor", kind, attempt + 1)
                            except UserError as error:
                                if type(error) is not UserError:
                                    raise
                                second_cr.rollback()
                                result["blocked"] = str(error)
                                return
                    raise AssertionError(f"Financial {kind} race exhausted fresh-transaction retries")
                except BaseException as error:
                    result["error"] = error
                    preloaded.set()

            thread = threading.Thread(target=worker, daemon=True, name=f"financial-{kind}-rr")
            thread.start()
            committed = False
            try:
                if not preloaded.wait(10):
                    frame = sys._current_frames().get(thread.ident)
                    self.fail("Concurrent transaction did not preload: " + "".join(traceback.format_stack(frame)))
                if "error" in result:
                    raise result["error"]
                self.assertEqual(result["attempts"][0]["physical"], {
                    "delivered": 70, "returned": 10 if kind == "credit" else 0,
                })
                self.assertEqual(result["attempts"][0]["financial"]["credit_reserved"], 0)
                self.assertEqual(result["attempts"][0]["stock"], 10 if kind == "credit" else 0)
                enter_guard.set()
                deadline = time.monotonic() + 10
                with self.registry.cursor() as observer:
                    while time.monotonic() < deadline:
                        observer.execute("SELECT pg_stat_clear_snapshot()")
                        observer.execute(
                            "SELECT wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid = %s",
                            [result["attempts"][0]["pid"]],
                        )
                        row = observer.fetchone()
                        if row and row[0] == "Lock" and first_pid in row[1]:
                            result["observed_lock_wait"] = True
                            _logger.info("Financial %s race observed worker PID %s waiting on first PID %s before commit",
                                         kind, result["attempts"][0]["pid"], first_pid)
                            break
                        if not thread.is_alive():
                            break
                        stop.wait(0.01)
                if "error" in result:
                    raise result["error"]
                self.assertTrue(result.get("observed_lock_wait"), f"No real first-transaction lock wait: {result}")
                first_cr.commit()
                committed = True
            finally:
                if not committed:
                    stop.set()
                enter_guard.set()
                first_cr.rollback()
                thread.join(25)
            self.assertFalse(thread.is_alive(), "Concurrent transaction failed bounded cleanup")
        if "error" in result:
            raise result["error"]
        self.assertTrue(result.get("observed_lock_wait"), result)
        self.assertGreaterEqual(result["retries"], 1, result)
        self.assertGreaterEqual(len(result["attempts"]), 2, result)
        self.assertNotEqual(result["attempts"][0]["txid"], result["attempts"][1]["txid"])
        self.assertNotIn("success", result)
        self.assertIn("blocked", result)
        with self.registry.cursor() as cr:
            env = self.environment(cr, actor)
            result["final"] = self.snapshot(env, fixture, second)
        _logger.info("Financial %s race winner %s final physical=%s financial=%s stock=%s retries=%s rejected=%s",
                     kind, first, result["final"]["physical"], result["final"]["financial"],
                     result["final"]["stock"], result["retries"], result["blocked"])
        return result

    def assert_invoice_race(self, first):
        result = self.race("invoice", first)
        self.assertIn("exceeds delivered, unreserved sales quantity", result["blocked"])
        initial, retried, final = result["attempts"][0], result["attempts"][1], result["final"]
        quantity = (40, 35)[first]
        self.assertEqual(initial["financial"]["invoice_reserved"], 0)
        self.assertEqual(initial["account_lines"], [])
        self.assertEqual(retried["financial"]["invoice_reserved"], quantity)
        self.assertEqual(final["physical"], {"delivered": 70, "returned": 0})
        self.assertEqual(final["financial"]["invoice_reserved"], quantity)
        self.assertEqual(final["financial"]["invoice_posted"], 0)
        self.assertEqual(final["financial"]["signed_reserved"], quantity)
        self.assertEqual(final["financial"]["credit_reserved"], 0)
        self.assertEqual(final["financial"]["invalid_line_ids"], [])
        self.assertEqual(len(final["account_lines"]), 1)
        self.assertEqual(final["account_lines"][0][1:], (result["winning_id"], quantity))
        self.assertLessEqual(final["financial"]["invoice_reserved"], 70)
        self.assertEqual(final["stock"], 0)

    def assert_credit_race(self, first):
        result = self.race("credit", first)
        self.assertIn("exceeds the remaining original-priced return allowance", result["blocked"])
        fixture = result["fixture"]
        initial, retried, final = result["attempts"][0], result["attempts"][1], result["final"]
        self.assertEqual(initial["financial"]["invoice_reserved"], 70)
        self.assertEqual(initial["financial"]["invoice_posted"], 70)
        self.assertEqual(initial["available_credit"], 10)
        self.assertEqual(len(initial["account_lines"]), 2)
        self.assertEqual({row[1] for row in initial["account_lines"]}, set(fixture["original_ids"]))
        self.assertEqual(initial["original_line_id"], fixture["original_line_ids"][1 - first])
        self.assertEqual(retried["available_credit"], 3)
        self.assertEqual(retried["financial"]["credit_reserved"], 7)
        self.assertEqual(final["physical"], {"delivered": 70, "returned": 10})
        self.assertEqual(final["financial"]["invoice_reserved"], 70)
        self.assertEqual(final["financial"]["invoice_posted"], 70)
        self.assertEqual(final["financial"]["credit_reserved"], 7)
        self.assertEqual(final["financial"]["credit_posted"], 0)
        self.assertEqual(final["financial"]["signed_reserved"], 63)
        self.assertEqual(final["financial"]["invalid_line_ids"], [])
        self.assertEqual(final["available_credit"], 3)
        self.assertEqual(len(final["account_lines"]), 3)
        self.assertEqual(final["stock"], 10)
        for contender, original_id in enumerate(fixture["original_ids"]):
            original = final["financial"]["originals"][original_id]
            self.assertEqual(original["invoice_posted"], (40, 30)[contender])
            self.assertEqual(original["credit_reserved"], 7 if contender == first else 0)
            self.assertLessEqual(original["credit_reserved"], original["invoice_posted"])
        with self.registry.cursor() as cr:
            env = self.environment(cr, self.billing_id)
            credits = env["account.move"].search([
                ("invoice_line_ids.sale_line_ids", "in", [fixture["line_id"]]),
                ("move_type", "=", "out_refund"), ("state", "!=", "cancel"),
            ])
            self.assertEqual(credits.ids, [result["winning_id"]])
            self.assertEqual(credits.reversed_entry_id.id, fixture["original_ids"][first])
            self.assertEqual(credits.invoice_line_ids.quantity, 7)
            self.assertEqual(credits.amount_untaxed, 595)
            self.assertEqual(credits.create_uid.id, self.billing_id)

    def assert_return_race(self, first):
        result = self.race("return", first)
        self.assertIn("Cumulative completed returns cannot exceed the original actual delivery quantity", result["blocked"])
        fixture = result["fixture"]
        initial, retried, final = result["attempts"][0], result["attempts"][1], result["final"]
        self.assertEqual(initial["physical"], {"delivered": 70, "returned": 0})
        old_states = {move_id: (state, quantity) for move_id, state, quantity in initial["moves"]}
        self.assertEqual(old_states[fixture["origin_id"]], ("done", 70))
        self.assertEqual(sum(state == "done" for state, quantity in old_states.values()), 1)
        self.assertEqual(retried["physical"], {"delivered": 70, "returned": 40})
        self.assertEqual(retried["stock"], 40)
        self.assertEqual(final["physical"], {"delivered": 70, "returned": 40})
        self.assertEqual(final["physical"]["delivered"] - final["physical"]["returned"], 30)
        self.assertEqual(final["stock"], 40)
        self.assertEqual(final["financial"]["invoice_reserved"], 0)
        self.assertEqual(final["financial"]["credit_reserved"], 0)
        with self.registry.cursor() as cr:
            env = self.environment(cr, self.warehouse_id)
            pickings = env["stock.picking"].browse(fixture["return_ids"])
            done = pickings.filtered(lambda picking: picking.state == "done")
            pending = pickings - done
            self.assertEqual(done.ids, [fixture["return_ids"][first]])
            self.assertEqual(pending.ids, [fixture["return_ids"][1 - first]])
            self.assertNotIn(pending.state, ("done", "cancel"))
            self.assertEqual(done.move_ids.quantity, 40)
            self.assertEqual(sum(done.move_ids.move_line_ids.mapped("quantity")), 40)
            self.assertEqual(pending.move_ids.quantity, 40)
            self.assertEqual(pending.move_ids.product_uom_qty, 40)
            self.assert_return_identity(done, fixture, 40)
            self.assert_return_identity(pending, fixture, 40)
            self.assertEqual(len(env["stock.move"].search([
                ("origin_returned_move_id", "=", fixture["origin_id"]), ("state", "=", "done"),
            ])), 1)
            internal = env["stock.quant"].search([
                ("product_id", "=", fixture["product_id"]), ("location_id.usage", "=", "internal"),
            ])
            self.assertTrue(all(quant.quantity >= 0 for quant in internal))

    def test_invoice_forty_wins_old_snapshot_thirty_five_retries_and_rejects(self):
        self.assert_invoice_race(first=0)

    def test_invoice_thirty_five_wins_old_snapshot_forty_retries_and_rejects(self):
        self.assert_invoice_race(first=1)

    def test_credit_original_forty_wins_global_return_seven_retries_and_rejects(self):
        self.assert_credit_race(first=0)

    def test_credit_original_thirty_wins_global_return_seven_retries_and_rejects(self):
        self.assert_credit_race(first=1)

    def test_return_first_picking_wins_old_snapshot_forty_retries_and_rejects(self):
        self.assert_return_race(first=0)

    def test_return_second_picking_wins_old_snapshot_forty_retries_and_rejects(self):
        self.assert_return_race(first=1)
