"""Real independent-transaction races; these deliberately commit isolated fixtures.

Only the dedicated integration database may run these tests. Retained immutable
audit events are test evidence; users are archived at class cleanup.
"""
import logging
import sys
import threading
import time
import traceback
import uuid

from odoo import SUPERUSER_ID, Command, api
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from psycopg2.errors import SerializationFailure

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install")
class TestSaleApprovalConcurrency(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls.cr.dbname != "odoo_portfolio_m1_test":
            raise AssertionError("Committed concurrency fixtures require odoo_portfolio_m1_test")
        with cls.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env["res.company"].create({"name": f"Approval concurrency {uuid.uuid4().hex[:8]}"})
            env = env(context={"allowed_company_ids": company.ids, "tracking_disable": True})
            company = env["res.company"].browse(company.id)
            company.write({"portfolio_approval_enabled": True})
            users = []
            for role, group in (("sales", "group_sale_salesman"), ("manager", "group_sale_manager")):
                users.append(new_test_user(
                    env, login=f"race_{role}_{uuid.uuid4().hex}", groups=f"sales_team.{group}",
                    company_id=company.id, company_ids=[Command.set(company.ids)],
                ).id)
            partner = env["res.partner"].create({"name": "Concurrency customer", "company_id": company.id})
            product = env["product.product"].create({
                "name": "Concurrency bearing", "type": "service", "company_id": company.id,
                "taxes_id": [Command.clear()],
            })
            cls.company_id, cls.sales_id, cls.manager_id = company.id, *users
            cls.partner_id, cls.product_id = partner.id, product.id
            cr.commit()
        cls.addClassCleanup(cls._archive_users)

    @classmethod
    def _archive_users(cls):
        with cls.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {"allowed_company_ids": [cls.company_id]})
            env["res.users"].browse([cls.sales_id, cls.manager_id]).write({"active": False})
            cr.commit()

    def environment(self, cr, uid=None):
        # The post-install runner owns Registry._lock in the main thread. Supply
        # the already loaded registry to this NEW transaction so a worker need
        # not try to load it again while that runner waits for the worker.
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, uid or self.sales_id, {
            "allowed_company_ids": [self.company_id], "tracking_disable": True,
            "mail_create_nosubscribe": True,
        })

    def make_order(self, approved=True):
        with self.registry.cursor() as cr:
            env = self.environment(cr)
            order = env["sale.order"].create({
                "partner_id": self.partner_id, "user_id": self.sales_id,
                "order_line": [Command.create({
                    "product_id": self.product_id, "product_uom_qty": 1,
                    "price_unit": 50000, "tax_id": [Command.clear()],
                }) for _ in range(2)],
            })
            order.action_portfolio_submit()
            if approved:
                order.with_user(self.manager_id).action_portfolio_approve()
            order_id = order.id
            cr.commit()
        return order_id

    def race(self, order_id, first, second, second_uid=None):
        """Run first, observe second waiting on its real DB lock, commit, retry.

        Preloading the second cursor deliberately models a stale repeatable-read
        request. A fresh transaction is mandatory after SerializationFailure.
        """
        result = {"retries": 0}
        started = threading.Event()
        with self.registry.cursor() as first_cr:
            first_env = self.environment(first_cr)
            first(first_env["sale.order"].browse(order_id))

            def worker():
                try:
                    for attempt in range(5):
                        with self.registry.cursor() as second_cr:
                            second_env = self.environment(second_cr, second_uid)
                            order = second_env["sale.order"].browse(order_id)
                            order.read(["state", "order_line", "portfolio_approval_state"])
                            order.order_line.read(["price_unit", "product_uom_qty"])
                            second_cr.execute("SELECT pg_backend_pid()")
                            result["pid"] = second_cr.fetchone()[0]
                            started.set()
                            try:
                                second(order)
                                second_cr.commit()
                                result["success"] = True
                                return
                            except SerializationFailure:
                                second_cr.rollback()
                                result["retries"] += 1
                                _logger.info("Approval race retried serialization failure (attempt %s)", attempt + 1)
                            except UserError as error:
                                second_cr.rollback()
                                result["blocked"] = str(error)
                                return
                    raise AssertionError("Concurrency operation exhausted serialization retries")
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
            order = self.environment(cr)["sale.order"].browse(order_id)
            result["final_state"] = order.state
            result["final_approval"] = order.portfolio_approval_state
        return result

    def mutation(self, kind):
        if kind == "create":
            return lambda order: order.env["sale.order.line"].create({
                "order_id": order.id, "product_id": self.product_id,
                "price_unit": 10, "product_uom_qty": 1,
            })
        if kind == "write":
            return lambda order: order.order_line[:1].write({"price_unit": 60000})
        return lambda order: order.order_line[:1].unlink()

    def test_confirmation_waits_for_line_mutations_then_rechecks(self):
        for kind in ("create", "write", "unlink"):
            with self.subTest(kind=kind):
                result = self.race(self.make_order(), self.mutation(kind), lambda order: order.action_confirm())
                self.assertIn("blocked", result)
                self.assertGreaterEqual(result["retries"], 1)
                self.assertEqual(result["final_state"], "draft")
                self.assertEqual(result["final_approval"], "draft")

    def test_line_mutations_wait_for_confirmation_then_recheck(self):
        for kind in ("create", "write", "unlink"):
            with self.subTest(kind=kind):
                result = self.race(self.make_order(), lambda order: order.action_confirm(), self.mutation(kind))
                self.assertIn("blocked", result)
                self.assertGreaterEqual(result["retries"], 1)
                self.assertEqual(result["final_state"], "sale")

    def test_approval_and_edit_are_serialized_in_both_orders(self):
        result = self.race(
            self.make_order(approved=False), self.mutation("write"),
            lambda order: order.action_portfolio_approve(), self.manager_id,
        )
        self.assertIn("blocked", result)
        self.assertGreaterEqual(result["retries"], 1)
        result = self.race(
            self.make_order(approved=False),
            lambda order: order.with_user(self.manager_id).action_portfolio_approve(), self.mutation("write"),
        )
        self.assertTrue(result.get("success"))
        self.assertGreaterEqual(result["retries"], 1)
        self.assertEqual(result["final_approval"], "draft")

    def test_duplicate_concurrent_approval_produces_one_decision(self):
        order_id = self.make_order(approved=False)
        result = self.race(
            order_id, lambda order: order.with_user(self.manager_id).action_portfolio_approve(),
            lambda order: order.action_portfolio_approve(), self.manager_id,
        )
        self.assertIn("blocked", result)
        with self.registry.cursor() as cr:
            order = self.environment(cr)["sale.order"].browse(order_id)
            self.assertEqual(len(order.portfolio_approval_history_ids.filtered(lambda event: event.event == "approved")), 1)

    def test_line_move_and_confirmation_are_serialized(self):
        target_id = self.make_order()
        def move(order):
            return order.order_line[:1].write({"order_id": target_id})

        result = self.race(self.make_order(), move, lambda order: order.action_confirm())
        self.assertIn("blocked", result)
        self.assertEqual(result["final_approval"], "draft")
        result = self.race(self.make_order(), lambda order: order.action_confirm(), move)
        self.assertIn("blocked", result)
        self.assertEqual(result["final_state"], "sale")

    def test_policy_and_confirmation_are_serialized_in_both_orders(self):
        def change_policy(order):
            company = order.with_user(SUPERUSER_ID).company_id
            company.write({"portfolio_approval_discount": company.portfolio_approval_discount + 1})

        result = self.race(self.make_order(), change_policy, lambda order: order.action_confirm())
        self.assertIn("blocked", result)
        self.assertGreaterEqual(result["retries"], 1)
        result = self.race(self.make_order(), lambda order: order.action_confirm(), change_policy)
        self.assertTrue(result.get("success"))

