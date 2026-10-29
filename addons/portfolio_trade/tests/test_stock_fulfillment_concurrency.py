"""Two different native SO deliveries contend for the same physical stock.

These deliberately commit isolated fixtures only in the dedicated integration
database. Completed native moves remain as evidence; fixture users are archived.
"""
import logging
import sys
import threading
import time
import traceback
import unittest
import uuid

from odoo import SUPERUSER_ID, Command, api
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from psycopg2.errors import SerializationFailure

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install", "portfolio_stock")
class TestStockFulfillmentConcurrency(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls.cr.dbname != "odoo_portfolio_m1_test":
            raise AssertionError("Committed concurrency fixtures require odoo_portfolio_m1_test")
        with cls.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env["res.company"].create({
                "name": f"Physical stock race {uuid.uuid4().hex[:8]}",
                "portfolio_fulfillment_enabled": True,
            })
            env.user.write({"company_ids": [Command.link(company.id)]})
            env = env(context={"allowed_company_ids": company.ids, "tracking_disable": True})
            users = []
            for role, groups in (
                ("sales", "sales_team.group_sale_salesman"),
                ("warehouse", "stock.group_stock_user"),
            ):
                users.append(new_test_user(
                    env, login=f"stock_race_{role}_{uuid.uuid4().hex}", groups=groups,
                    company_id=company.id, company_ids=[Command.set(company.ids)],
                ).id)
            customer = env["res.partner"].create({"name": "Physical race customer", "ref": "RACE-PHYS-C"})
            warehouse = env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
            cls.company_id, cls.sales_id, cls.warehouse_id = company.id, *users
            cls.customer_id, cls.location_id = customer.id, warehouse.lot_stock_id.id
            cr.commit()
        cls.addClassCleanup(cls._archive_users)

    @classmethod
    def _archive_users(cls):
        with cls.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {"allowed_company_ids": [cls.company_id]})
            env["res.users"].browse([cls.sales_id, cls.warehouse_id]).write({"active": False})
            cr.commit()

    def environment(self, cr, uid=None):
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, uid or self.warehouse_id, {
            "allowed_company_ids": [self.company_id], "tracking_disable": True,
            "mail_create_nosubscribe": True,
        })

    def make_competing_deliveries(self):
        with self.registry.cursor() as cr:
            env = self.environment(cr, SUPERUSER_ID)
            product = env["product.product"].create({
                "name": f"Shared physical bearing {uuid.uuid4().hex[:8]}", "default_code": "RACE-PHYS",
                "type": "consu", "is_storable": True, "list_price": 100,
                "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
            })
            quant = env["stock.quant"].with_context(inventory_mode=True).create({
                "product_id": product.id, "location_id": self.location_id, "inventory_quantity": 40,
            })
            quant.action_apply_inventory()
            orders = env["sale.order"].with_user(self.sales_id).create([{
                "partner_id": self.customer_id, "company_id": self.company_id, "user_id": self.sales_id,
                "order_line": [Command.create({
                    "product_id": product.id, "product_uom_qty": 30, "price_unit": 100,
                    "tax_id": [Command.clear()],
                })],
            } for _ in range(2)])
            orders.action_confirm()
            self.assertEqual(len(orders), 2)
            moves = orders.mapped("picking_ids.move_ids").with_user(self.warehouse_id)
            self.assertEqual(len(moves), 2)
            moves.write({"quantity": 30, "picked": True})
            move_ids, product_id = moves.ids, product.id
            cr.commit()
        return move_ids, product_id

    def race(self, reverse=False):
        move_ids, product_id = self.make_competing_deliveries()
        if reverse:
            move_ids.reverse()
        result = {"retries": 0}
        started = threading.Event()
        with self.registry.cursor() as first_cr:
            first_env = self.environment(first_cr)
            first_env["stock.move"].browse(move_ids[0])._action_done()

            def worker():
                try:
                    for attempt in range(5):
                        with self.registry.cursor() as second_cr:
                            env = self.environment(second_cr)
                            move = env["stock.move"].browse(move_ids[1])
                            move.read(["state", "quantity", "company_id", "move_line_ids"])
                            move.move_line_ids.read(["quantity", "location_id", "lot_id", "package_id", "owner_id"])
                            quants = env["stock.quant"].search([
                                ("product_id", "=", product_id), ("location_id", "=", self.location_id),
                            ])
                            quants.read(["quantity", "reserved_quantity", "lot_id", "package_id", "owner_id"])
                            if attempt == 0:
                                result["preloaded_stock"] = sum(quants.mapped("quantity"))
                                result["preloaded_move"] = move.state
                            second_cr.execute("SELECT pg_backend_pid()")
                            result["pid"] = second_cr.fetchone()[0]
                            started.set()
                            try:
                                move._action_done()
                                second_cr.commit()
                                result["success"] = True
                                return
                            except SerializationFailure:
                                second_cr.rollback()
                                result["retries"] += 1
                                _logger.info("Physical stock race retried serialization failure (attempt %s)", attempt + 1)
                            except UserError as error:
                                second_cr.rollback()
                                result["blocked"] = str(error)
                                return
                    raise AssertionError("Physical stock race exhausted fresh-transaction retries")
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
        with self.registry.cursor() as cr:
            env = self.environment(cr)
            quants = env["stock.quant"].search([
                ("product_id", "=", product_id), ("location_id", "=", self.location_id),
            ])
            moves = env["stock.move"].browse(move_ids)
            result["remaining"] = sum(quants.mapped("quantity"))
            result["completed"] = sum(moves.filtered(lambda move: move.state == "done").mapped("quantity"))
            result["done_count"] = len(moves.filtered(lambda move: move.state == "done"))
            self.assertTrue(all(quant.quantity >= 0 for quant in quants))
        return result

    def test_distinct_sales_orders_wait_retry_and_reject_overdelivery(self):
        result = self.race()
        self.assertEqual(result["preloaded_stock"], 40)
        self.assertGreaterEqual(result["retries"], 1, result)
        self.assertIn("blocked", result)
        self.assertEqual(result["remaining"], 10)
        self.assertEqual(result["completed"], 30)
        self.assertEqual(result["done_count"], 1)

    def test_reverse_delivery_order_retains_same_physical_invariant(self):
        result = self.race(reverse=True)
        self.assertEqual(result["preloaded_stock"], 40)
        self.assertGreaterEqual(result["retries"], 1, result)
        self.assertIn("blocked", result)
        self.assertEqual(result["remaining"], 10)
        self.assertEqual(result["completed"], 30)
        self.assertEqual(result["done_count"], 1)

    def test_sales_actor_cannot_commit_physical_completion_under_sudo(self):
        move_ids, product_id = self.make_competing_deliveries()
        with self.registry.cursor() as cr:
            env = self.environment(cr, self.sales_id)
            with cr.savepoint():
                env.invalidate_all()
                with unittest.TestCase.assertRaises(self, AccessError):
                    env["stock.move"].browse(move_ids[0]).sudo()._action_done()
                env.flush_all()
                env.invalidate_all()
                self.assertEqual(sum(env["stock.quant"].search([
                    ("product_id", "=", product_id), ("location_id", "=", self.location_id),
                ]).mapped("quantity")), 40)
                self.assertFalse(env["stock.move"].browse(move_ids).filtered(lambda move: move.state == "done"))
