"""Committed, independent-transaction tests of the cross-company source invariant."""
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

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install", "portfolio_procurement")
class TestProcurementProvenanceConcurrency(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls.cr.dbname != "odoo_portfolio_m1_test":
            raise AssertionError("Committed concurrency fixtures require odoo_portfolio_m1_test")
        with cls.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            companies = env["res.company"].create([
                {"name": f"Provenance race A {uuid.uuid4().hex[:8]}"},
                {"name": f"Provenance race B {uuid.uuid4().hex[:8]}"},
            ])
            env = env(context={"allowed_company_ids": companies.ids, "tracking_disable": True})
            actor = new_test_user(
                env, login=f"provenance_race_actor_{uuid.uuid4().hex}",
                groups="sales_team.group_sale_salesman,purchase.group_purchase_user",
                company_id=companies[0].id, company_ids=[Command.set(companies.ids)],
            )
            customer = env["res.partner"].create({"name": "Provenance race customer", "ref": "RACE-C"})
            vendor = env["res.partner"].create({"name": "Provenance race vendor", "ref": "RACE-V"})
            product = env["product.product"].create({
                "name": "Shared provenance race service", "type": "service", "default_code": "RACE-SVC",
                "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
            })
            warehouse_b = env["stock.warehouse"].search([("company_id", "=", companies[1].id)], limit=1)
            cls.company_a_id, cls.company_b_id = companies.ids
            cls.actor_id, cls.customer_id, cls.vendor_id = actor.id, customer.id, vendor.id
            cls.product_id, cls.warehouse_b_id = product.id, warehouse_b.id
            cr.commit()
        cls.addClassCleanup(cls._archive_actor)

    @classmethod
    def _archive_actor(cls):
        with cls.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {
                "allowed_company_ids": [cls.company_a_id, cls.company_b_id],
            })
            env["res.users"].browse(cls.actor_id).write({"active": False})
            cr.commit()

    def environment(self, cr):
        # Reuse the runner's loaded registry, without its main-thread registry lock.
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, self.actor_id, {
            "allowed_company_ids": [self.company_a_id, self.company_b_id],
            "tracking_disable": True, "mail_create_nosubscribe": True,
        })

    def make_source(self):
        with self.registry.cursor() as cr:
            env = self.environment(cr)
            companies = env["res.company"].browse([self.company_a_id, self.company_b_id])
            self.assertFalse(any(companies.mapped("portfolio_masterdata_enabled")))
            self.assertFalse(any(companies.mapped("portfolio_approval_enabled")))
            self.assertFalse(any(companies.mapped("portfolio_fulfillment_enabled")))
            source = env["sale.order"].create({
                "partner_id": self.customer_id, "user_id": self.actor_id, "company_id": self.company_a_id,
                "order_line": [Command.create({
                    "product_id": self.product_id, "product_uom_qty": 1, "price_unit": 100,
                    "tax_id": [Command.clear()],
                })],
            })
            source.action_confirm()
            self.assertEqual(source.state, "sale")
            source_id = source.id
            cr.commit()
        return source_id

    def perform(self, env, source_id, operation):
        source = env["sale.order"].browse(source_id)
        if operation == "transfer":
            return source.with_company(self.company_b_id).write({
                "company_id": self.company_b_id, "warehouse_id": self.warehouse_b_id,
            })
        purchase = env["purchase.order"].create({
            "partner_id": self.vendor_id, "company_id": self.company_a_id,
            "portfolio_source_sale_id": source_id,
            "order_line": [Command.create({
                "product_id": self.product_id, "product_qty": 1, "price_unit": 60,
                "product_uom": env["product.product"].browse(self.product_id).uom_po_id.id,
                "date_planned": fields.Datetime.now(), "taxes_id": [Command.clear()],
            })],
        })
        purchase.button_confirm()
        self.assertEqual(purchase.state, "purchase")
        return purchase

    def race(self, first_operation, second_operation):
        source_id = self.make_source()
        result = {"retries": 0}
        started = threading.Event()
        with self.registry.cursor() as first_cr:
            self.perform(self.environment(first_cr), source_id, first_operation)

            def worker():
                try:
                    for attempt in range(5):
                        with self.registry.cursor() as second_cr:
                            env = self.environment(second_cr)
                            source = env["sale.order"].browse(source_id)
                            source.read(["company_id", "state", "portfolio_revision"])
                            purchases = env["purchase.order"].search_count([
                                ("portfolio_source_sale_id", "=", source_id),
                            ])
                            if attempt == 0:
                                result["preloaded_company"] = source.company_id.id
                                result["preloaded_purchases"] = purchases
                            second_cr.execute("SELECT pg_backend_pid()")
                            result["pid"] = second_cr.fetchone()[0]
                            started.set()
                            try:
                                self.perform(env, source_id, second_operation)
                                second_cr.commit()
                                result["success"] = True
                                return
                            except SerializationFailure:
                                second_cr.rollback()
                                result["retries"] += 1
                                _logger.info("Provenance race retried serialization failure (attempt %s)", attempt + 1)
                            except UserError as error:
                                second_cr.rollback()
                                result["blocked"] = str(error)
                                return
                    raise AssertionError("Provenance race exhausted fresh-transaction retries")
                except BaseException as error:
                    result["error"] = error

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            try:
                if not started.wait(10):
                    frame = sys._current_frames().get(thread.ident)
                    self.fail("Concurrent transaction did not start: " + "".join(traceback.format_stack(frame)))
                deadline = time.monotonic() + 10
                with self.registry.cursor() as observer:
                    while time.monotonic() < deadline:
                        observer.execute("SELECT pg_stat_clear_snapshot()")
                        observer.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [result["pid"]])
                        row = observer.fetchone()
                        if row and row[0] == "Lock":
                            result["observed_lock_wait"] = True
                            break
                        if not thread.is_alive():
                            break
                        threading.Event().wait(0.01)
                self.assertTrue(result.get("observed_lock_wait"), f"No real lock wait observed: {result}")
                first_cr.commit()
            finally:
                first_cr.rollback()
                thread.join(15)
            self.assertFalse(thread.is_alive(), "Concurrent transaction failed to finish")
        if "error" in result:
            raise result["error"]
        self.assertEqual(result["preloaded_company"], self.company_a_id)
        self.assertEqual(result["preloaded_purchases"], 0)
        with self.registry.cursor() as cr:
            env = self.environment(cr)
            source = env["sale.order"].browse(source_id)
            purchases = env["purchase.order"].search([("portfolio_source_sale_id", "=", source_id)])
            result["final_company"] = source.company_id.id
            result["purchase_count"] = len(purchases)
            result["same_company"] = all(purchase.company_id == source.company_id for purchase in purchases)
        return result

    def test_source_transfer_waits_for_attachment_and_rechecks_in_new_transaction(self):
        result = self.race("attach", "transfer")
        self.assertGreaterEqual(result["retries"], 1, result)
        self.assertIn("blocked", result)
        self.assertEqual(result["final_company"], self.company_a_id)
        self.assertEqual(result["purchase_count"], 1)
        self.assertTrue(result["same_company"])

    def test_source_attachment_waits_for_transfer_and_rechecks_in_new_transaction(self):
        result = self.race("transfer", "attach")
        self.assertGreaterEqual(result["retries"], 1, result)
        self.assertIn("blocked", result)
        self.assertEqual(result["final_company"], self.company_b_id)
        self.assertEqual(result["purchase_count"], 0)
        self.assertTrue(result["same_company"])
