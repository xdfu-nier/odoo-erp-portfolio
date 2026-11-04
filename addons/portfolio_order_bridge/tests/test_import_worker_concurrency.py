"""Committed worker competition and actual process-exit recovery evidence.

Only the acceptance database may receive these UUID-owned native fixtures.
The native M1 company lock serializes commercial writes in one company; the
SKIP LOCKED case therefore holds only job rows in a separate technical cursor.
"""

import json
import logging
import os
import stat
import subprocess
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from odoo import SUPERUSER_ID, Command, api
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from psycopg2.errors import SerializationFailure

from ..payload import normalize_order_payload

_logger = logging.getLogger(__name__)
PROJECT = Path(__file__).resolve().parents[3]
MAIN_PROJECT = PROJECT.parents[1] if PROJECT.parent.name == ".worktrees" else PROJECT


@tagged("post_install", "-at_install", "portfolio_import_worker_concurrency")
class TestImportWorkerConcurrency(TransactionCase):
    JOB = "portfolio.import.job"
    SOURCE = "portfolio.import.source"
    ATTEMPT = "portfolio.import.attempt"
    INTEGRATION = "portfolio_order_bridge.group_import_integration"
    OPERATOR = "portfolio_order_bridge.group_import_operator"
    SALES = "sales_team.group_sale_salesman"
    FORBIDDEN = (
        "base.group_system", "base.group_erp_manager", "sales_team.group_sale_manager",
        "sales_team.group_sale_salesman_all_leads", "purchase.group_purchase_user",
        "stock.group_stock_user", "account.group_account_invoice",
    )
    RECEPTION = [
        "raw_payload", "canonical_payload", "payload_hash", "received_at", "submitted_by_id",
        "source_id", "company_id", "external_order_id", "create_uid", "create_date",
    ]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # No committed fixture cursor or child may precede this exact guard.
        if cls.cr.dbname != "odoo_portfolio_m1_test":
            raise AssertionError("Committed worker fixtures require odoo_portfolio_m1_test")
        cls.fixtures = []
        cls.addClassCleanup(cls._archive_fixtures)

    @classmethod
    def _archive_fixtures(cls):
        for fixture in cls.fixtures:
            with cls.registry.cursor() as cr:
                if cr.transaction is None:
                    cr.transaction = api.Transaction(cls.registry)
                context = {"allowed_company_ids": [fixture["company_id"]]}
                settings = api.Environment(cr, fixture["settings_id"], context)
                if settings.su or settings.user.share:
                    raise AssertionError("Source archival requires an ordinary internal Settings actor")
                # Source validation needs active configured users until archival.
                settings[cls.SOURCE].browse(fixture["source_id"]).write({"active": False})
                admin = api.Environment(cr, SUPERUSER_ID, context)
                admin["res.users"].browse(fixture["actor_ids"]).write({"active": False})
                cr.commit()

    def environment(self, cr, uid, fixture):
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, uid, {
            "allowed_company_ids": [fixture["company_id"]], "lang": "en_US",
            "tracking_disable": True, "mail_create_nosubscribe": True,
        })

    def bounded(self, cr):
        cr.execute("SELECT set_config('lock_timeout', '15000', true), set_config('statement_timeout', '20000', true)")

    def assert_actor(self, env, fixture, role):
        self.assertEqual(env.uid, fixture[f"{role}_id"])
        self.assertNotEqual(env.uid, SUPERUSER_ID)
        self.assertFalse(env.su)
        self.assertTrue(env.user.active)
        self.assertFalse(env.user.share)
        self.assertEqual(env.company.id, fixture["company_id"])
        self.assertEqual(env.companies.ids, [fixture["company_id"]])
        self.assertEqual(env.user.company_ids.ids, [fixture["company_id"]])
        if role == "settings":
            self.assertTrue(env.user.has_group("base.group_system"))
            return
        for group in self.FORBIDDEN:
            self.assertFalse(env.user.has_group(group), group)
        self.assertEqual(env.user.has_group(self.OPERATOR), role == "operator")
        self.assertEqual(env.user.has_group(self.INTEGRATION), role == "integration")
        self.assertEqual(env.user.has_group(self.SALES), role in {"sales", "operator"})

    def new_fixture(self):
        token = uuid.uuid4().hex
        fixture = {"token": token, "actor_ids": [], "jobs": [], "payloads": {}, "receptions": {}}
        with self.registry.cursor() as cr:
            cr.transaction = api.Transaction(self.registry)
            admin = api.Environment(cr, SUPERUSER_ID, {
                "disable_company_pricelist_creation": True, "tracking_disable": True,
            })
            employee = admin.ref("base.group_user")
            features = admin.ref("base.group_multi_currency") | admin.ref("product.group_product_pricelist")
            before = {
                group.id: (group in employee.implied_ids, sorted(group.with_context(active_test=False).users.ids))
                for group in features
            }
            pricelists = admin["product.pricelist"].with_context(active_test=False).search([]).ids
            cny = admin.ref("base.CNY")
            cny.active = True
            company = admin["res.company"].create({
                "name": f"Worker RR {token}", "currency_id": cny.id,
                "email": f"worker-{token}@example.invalid",
            })
            fixture["company_id"] = company.id
            for group in features:
                implied, members = before[group.id]
                if implied:
                    employee._apply_group(group)
                else:
                    employee._remove_group(group)
                if sorted(group.with_context(active_test=False).users.ids) != members:
                    group.with_context(active_test=False).write({"users": [Command.set(members)]})
                self.assertEqual(
                    (group in employee.implied_ids, sorted(group.with_context(active_test=False).users.ids)),
                    before[group.id],
                )
            self.assertEqual(admin["product.pricelist"].with_context(active_test=False).search([]).ids, pricelists)
            admin = admin(context={
                "allowed_company_ids": company.ids, "disable_company_pricelist_creation": True,
                "tracking_disable": True,
            })
            for role, groups in (
                ("settings", "base.group_system"), ("sales", self.SALES),
                ("operator", f"{self.OPERATOR},{self.SALES}"), ("integration", self.INTEGRATION),
            ):
                user = new_test_user(
                    admin, login=f"worker_rr_{role}_{token}", groups=groups,
                    company_id=company.id, company_ids=[Command.set(company.ids)],
                    email=f"worker-{role}-{token}@example.invalid", notification_type="inbox",
                    context={"no_reset_password": True},
                )
                fixture[f"{role}_id"] = user.id
                fixture["actor_ids"].append(user.id)
            self.assertNotEqual(fixture["sales_id"], fixture["operator_id"])
            self.assertEqual(admin["res.users"].browse(fixture["actor_ids"]).mapped("notification_type"), ["inbox"] * 4)
            warehouse = admin["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
            if not warehouse:
                warehouse = admin["stock.warehouse"].create({
                    "name": f"Worker RR warehouse {token}", "code": f"W{token[:4]}",
                    "company_id": company.id,
                })
            fixture["warehouse_id"] = warehouse.id
            fixture["pricelist_id"] = admin["product.pricelist"].create({
                "name": f"Worker RR CNY {token}", "company_id": company.id, "currency_id": cny.id,
            }).id
            settings = self.environment(cr, fixture["settings_id"], fixture)
            self.assert_actor(settings, fixture, "settings")
            settings["res.company"].browse(company.id).write({
                "portfolio_masterdata_enabled": True, "portfolio_approval_enabled": True,
                "portfolio_approval_amount": 10000, "portfolio_approval_discount": 10,
            })
            customer = admin["res.partner"].create({
                "name": f"Worker RR customer {token}", "ref": f"WC-{token}", "company_id": company.id,
            })
            supplier = admin["res.partner"].create({
                "name": f"Worker RR supplier {token}", "ref": f"WV-{token}", "company_id": company.id,
            })
            category = admin["uom.category"].create({"name": f"Worker RR exact {token}"})
            unit = admin["uom.uom"].create({
                "name": f"Worker RR half-unit {token}", "category_id": category.id,
                "uom_type": "reference", "rounding": 0.5,
            })
            products = admin["product.product"]
            for number in (1, 2):
                product = admin["product.product"].create({
                    "name": f"Worker RR item {number} {token}", "default_code": f"WP-{number}-{token}",
                    "company_id": company.id, "type": "consu", "is_storable": True,
                    "invoice_policy": "delivery", "uom_id": unit.id, "uom_po_id": unit.id,
                    "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
                })
                admin["product.supplierinfo"].create({
                    "product_tmpl_id": product.product_tmpl_id.id, "partner_id": supplier.id,
                    "company_id": company.id, "currency_id": cny.id, "price": 5,
                })
                products |= product
            source = settings[self.SOURCE].create({
                "name": f"Worker RR source {token}", "company_id": company.id,
                "company_code": f"WC-{token}", "code": f"WS-{token}",
                "pricelist_id": fixture["pricelist_id"], "warehouse_id": warehouse.id,
                "sales_user_id": fixture["sales_id"], "processor_user_id": fixture["operator_id"],
                "integration_user_ids": [Command.set([fixture["integration_id"]])],
            })
            fixture.update(source_id=source.id, company_code=source.company_code, source_code=source.code)
            fixture["customer_map_id"] = settings["portfolio.import.customer.map"].create({
                "source_id": source.id, "external_ref": "CUSTOMER", "partner_id": customer.id,
            }).id
            settings["portfolio.import.product.map"].create([
                {"source_id": source.id, "external_sku": f"SKU-{number}", "product_id": product.id}
                for number, product in enumerate(products, 1)
            ])
            for role in ("operator", "integration", "sales"):
                self.assert_actor(self.environment(cr, fixture[f"{role}_id"], fixture), fixture, role)
            cr.commit()
        self.fixtures.append(fixture)
        return fixture

    def submit_jobs(self, fixture, count=1):
        with self.registry.cursor() as cr:
            env = self.environment(cr, fixture["integration_id"], fixture)
            self.assert_actor(env, fixture, "integration")
            for _ in range(count):
                payload = {
                    "company_code": fixture["company_code"], "source_code": fixture["source_code"],
                    "external_order_id": f"WO-{uuid.uuid4().hex}", "customer_ref": "CUSTOMER",
                    "currency": "CNY", "request_id": "original-request", "sent_at": "2026-11-04T08:00:00Z",
                    "lines": [
                        {"external_line_id": "2", "sku": "SKU-2", "quantity": "1.5",
                         "unit_price": "12.50", "discount": "1.00"},
                        {"external_line_id": "1", "sku": "SKU-1", "quantity": "2",
                         "unit_price": "20", "discount": "0"},
                    ],
                }
                response = env[self.JOB].submit_order(payload)
                job = env[self.JOB].browse(response["job_id"])
                self.assertEqual((job.state, job.attempt_count), ("pending", 0))
                fixture["jobs"].append(job.id)
                fixture["payloads"][job.id] = payload
                fixture["receptions"][job.id] = job.read(self.RECEPTION)[0]
            cr.commit()
        return fixture["jobs"]

    def process_fresh(self, fixture):
        with self.registry.cursor() as cr:
            self.bounded(cr)
            env = self.environment(cr, fixture["operator_id"], fixture)
            self.assert_actor(env, fixture, "operator")
            count = env[self.JOB]._process_pending()
            cr.commit()
            return count

    def snapshot(self, env, fixture, job_id):
        job = env[self.JOB].browse(job_id)._worker_authorized_job()
        state = job.state
        env.cr.execute("SELECT pg_backend_pid(), txid_current(), current_setting('transaction_isolation')")
        pid, txid, isolation = env.cr.fetchone()
        self.assertEqual(isolation, "repeatable read")
        return {"pid": pid, "txid": txid, "state": state}

    def witness_lock(self, first, second, thread):
        deadline = time.monotonic() + 10
        with self.registry.cursor() as cr:
            cr.execute("SELECT pg_backend_pid()")
            observer_pid = cr.fetchone()[0]
            self.assertNotIn(observer_pid, [first["pid"], second["pid"]])
            while time.monotonic() < deadline:
                cr.execute("SELECT pg_stat_clear_snapshot()")
                cr.execute(
                    "SELECT wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid = %s",
                    [second["pid"]],
                )
                row = cr.fetchone()
                if row and row[0] == "Lock" and first["pid"] in row[1]:
                    _logger.info("Worker race observer PID %s saw PID %s blocked by PID %s before commit",
                                 observer_pid, second["pid"], first["pid"])
                    return observer_pid
                if not thread.is_alive():
                    break
                # Poll cadence is not evidence: the pg_stat_activity lock and
                # pg_blocking_pids witness above is required before releasing.
                threading.Event().wait(0.01)
        self.fail("No independently observed native lock wait before first commit")

    def race(self, fixture, *, retry=False):
        job_id = fixture["jobs"][0]
        held, release, preloaded, contend, stop = (threading.Event() for _ in range(5))
        result = {"snapshots": [], "serialization_codes": []}
        sale_class = type(self.env["sale.order"])
        real_create = sale_class.create

        def pause_first():
            held.set()
            if not release.wait(15):
                raise AssertionError("First worker checkpoint was not released")
            if stop.is_set():
                raise AssertionError("Worker race stopped before commit")

        @api.model_create_multi
        def pause_after_real_quote(records, values):
            quotes = real_create(records, values)
            if threading.current_thread() is first_thread:
                self.assert_actor(records.env, fixture, "operator")
                records.env.flush_all()
                job = records.env[self.JOB].browse(job_id)
                self.assertEqual((job.state, job.attempt_count), ("processing", 1))
                self.assertEqual(len(quotes.order_line), 2)
                result["uncommitted_quote_id"] = quotes.id
                result["uncommitted_line_ids"] = quotes.order_line.ids
                pause_first()
            return quotes

        def first_worker():
            try:
                with self.registry.cursor() as cr:
                    self.bounded(cr)
                    env = self.environment(cr, fixture["operator_id"], fixture)
                    self.assert_actor(env, fixture, "operator")
                    result["first_snapshot"] = self.snapshot(env, fixture, job_id)
                    if retry:
                        env[self.JOB].browse(job_id).action_retry("First operator repaired mapping")
                        env.flush_all()
                        pause_first()
                    else:
                        result["first_count"] = env[self.JOB]._process_pending()
                    if stop.is_set():
                        cr.rollback()
                        return
                    cr.commit()
            except BaseException as error:
                result["first_error"] = error
                held.set()

        def second_worker():
            try:
                for number in range(5):
                    if stop.is_set():
                        return
                    with self.registry.cursor() as cr:
                        self.bounded(cr)
                        env = self.environment(cr, fixture["operator_id"], fixture)
                        self.assert_actor(env, fixture, "operator")
                        result["snapshots"].append(self.snapshot(env, fixture, job_id))
                        if number == 0:
                            preloaded.set()
                            if not contend.wait(10):
                                raise AssertionError("Stale contender was not released")
                        try:
                            if retry:
                                env[self.JOB].browse(job_id).action_retry("Second operator alternative reason")
                                result["second_retry_succeeded"] = True
                            else:
                                result["second_count"] = env[self.JOB]._process_pending()
                            if stop.is_set():
                                cr.rollback()
                                return
                            cr.commit()
                            return
                        except SerializationFailure as error:
                            cr.rollback()
                            result["serialization_codes"].append(error.pgcode)
                        except UserError as error:
                            if not retry or type(error) is not UserError or str(error) != "Only a failed import job can be retried manually.":
                                raise
                            cr.rollback()
                            result["second_rejected_pending"] = True
                            return
                raise AssertionError("Worker exhausted fresh-cursor serialization retries")
            except BaseException as error:
                result["second_error"] = error
                preloaded.set()

        first_thread = threading.Thread(target=first_worker, daemon=True, name=f"worker-first-{job_id}")
        second_thread = threading.Thread(target=second_worker, daemon=True, name=f"worker-second-{job_id}")
        with patch.object(sale_class, "create", pause_after_real_quote) if not retry else nullcontext():
            first_thread.start()
            try:
                self.assertTrue(held.wait(15), "First worker did not reach the uncommitted native checkpoint")
                if "first_error" in result:
                    raise result["first_error"]
                second_thread.start()
                self.assertTrue(preloaded.wait(10), "Second worker did not acquire a stale RR snapshot")
                if "second_error" in result:
                    raise result["second_error"]
                first, second = result["first_snapshot"], result["snapshots"][0]
                self.assertEqual(second["state"], "failed" if retry else "pending")
                self.assertNotEqual(first["pid"], second["pid"])
                self.assertNotEqual(first["txid"], second["txid"])
                contend.set()
                result["observer_pid"] = self.witness_lock(first, second, second_thread)
                release.set()
                first_thread.join(25)
                second_thread.join(25)
            finally:
                stop.set()
                release.set()
                contend.set()
                first_thread.join(25)
                if second_thread.ident is not None:
                    second_thread.join(25)
                self.assertFalse(first_thread.is_alive(), "First worker did not stop within its SQL/event bounds")
                self.assertFalse(second_thread.is_alive(), "Second worker did not stop within its SQL/event bounds")
        for name in ("first_error", "second_error"):
            if name in result:
                raise result[name]
        self.assertTrue(result["observer_pid"])
        self.assertEqual(result["serialization_codes"], ["40001"] * len(result["serialization_codes"]))
        # Some lock acquisitions can retain a valid stale snapshot until the
        # changed job row is visited. A real 40001 is retried, never simulated.
        self.assertEqual(len({item["txid"] for item in result["snapshots"]}), len(result["snapshots"]))
        _logger.info("Worker race job %s first PID %s contender PIDs %s txids %s SQLSTATEs %s",
                     job_id, result["first_snapshot"]["pid"],
                     [item["pid"] for item in result["snapshots"]],
                     [item["txid"] for item in result["snapshots"]], result["serialization_codes"])
        return result

    def native_counts(self, cr, fixture):
        result = {}
        for table in (
            "sale_order", "sale_order_line", "portfolio_sale_approval", "stock_picking",
            "stock_move", "purchase_order", "account_move", "portfolio_import_attempt",
        ):
            # Fixed technical table identifiers; only a parameterized,
            # UUID-company-bounded read, with no business SQL writes.
            cr.execute(f"SELECT count(*) FROM {table} WHERE company_id = %s", [fixture["company_id"]])
            result[table] = cr.fetchone()[0]
        return result

    def assert_done(self, fixture, *, attempt_count=1, event_types=None):
        expected_events = event_types or ["done"]
        quote_ids = []
        for role in ("integration", "operator"):
            with self.registry.cursor() as cr:
                env = self.environment(cr, fixture[f"{role}_id"], fixture)
                self.assert_actor(env, fixture, role)
                jobs = env[self.JOB].search([("source_id", "=", fixture["source_id"])], order="id")
                self.assertEqual(jobs.ids, fixture["jobs"])
                for job in jobs:
                    self.assertEqual(job.read(self.RECEPTION)[0], fixture["receptions"][job.id])
                    normalized = normalize_order_payload(fixture["payloads"][job.id])
                    self.assertEqual((job.raw_payload, job.payload_hash), (normalized.raw_json, normalized.payload_hash))
                    self.assertEqual((job.state, job.attempt_count), ("done", attempt_count))
                    self.assertTrue(job.last_attempt_at)
                    self.assertFalse(job.error_code)
                    self.assertFalse(job.error_detail)
                    self.assertTrue(job.sale_order_id)
                    events = env[self.ATTEMPT].search([("job_id", "=", job.id)], order="id")
                    self.assertEqual(events.mapped("event_type"), expected_events)
                    self.assertEqual(events.mapped("actor_id").ids, [fixture["operator_id"]])
                    self.assertEqual(events[-1].attempt_no, attempt_count)
                    if role == "operator":
                        quote = job.sale_order_id
                        quote.check_access("read")
                        self.assertEqual(quote.state, "draft")
                        self.assertEqual(quote.user_id.id, fixture["sales_id"])
                        self.assertEqual(quote.create_uid.id, fixture["operator_id"])
                        self.assertEqual(quote.portfolio_creator_id.id, fixture["operator_id"])
                        self.assertEqual(quote.company_id.id, fixture["company_id"])
                        self.assertEqual(len(quote.order_line), 2)
                        self.assertFalse(quote.portfolio_approval_history_ids)
                        quote_ids.append(quote.id)
                if role == "operator":
                    counts = self.native_counts(cr, fixture)
                    self.assertEqual(counts["sale_order"], len(fixture["jobs"]))
                    self.assertEqual(counts["sale_order_line"], 2 * len(fixture["jobs"]))
                    self.assertEqual(counts["portfolio_import_attempt"], len(expected_events) * len(fixture["jobs"]))
                    for table in ("portfolio_sale_approval", "stock_picking", "stock_move", "purchase_order", "account_move"):
                        self.assertEqual(counts[table], 0, table)
        self.assertEqual(len(set(quote_ids)), len(fixture["jobs"]))
        with self.registry.cursor() as cr:
            sales = self.environment(cr, fixture["sales_id"], fixture)
            self.assert_actor(sales, fixture, "sales")
            quotes = sales["sale.order"].browse(quote_ids)
            self.assertEqual([row["id"] for row in quotes.read(["id"])], quote_ids)
            self.assertEqual(quotes.mapped("user_id").ids, [fixture["sales_id"]])
        return quote_ids

    def test_same_queue_workers_wait_then_create_one_native_quote(self):
        fixture = self.new_fixture()
        self.submit_jobs(fixture)
        result = self.race(fixture)
        self.assertEqual(result["first_count"], 1)
        self.assertEqual(result["second_count"], 0)
        self.assertEqual(self.assert_done(fixture), [result["uncommitted_quote_id"]])
        self.assertEqual(self.process_fresh(fixture), 0)
        self.assert_done(fixture)

    def test_skip_locked_selects_five_after_two_locked_earliest_jobs(self):
        fixture = self.new_fixture()
        job_ids = self.submit_jobs(fixture, count=7)
        # Authorization and ID selection happen under the ordinary operator;
        # the following SQL cursor holds ONLY the first two job rows, never a
        # company/source lock or _worker_lock_job().
        with self.registry.cursor() as cr:
            env = self.environment(cr, fixture["operator_id"], fixture)
            self.assert_actor(env, fixture, "operator")
            self.assertEqual(env[self.JOB].search([("state", "=", "pending")], order="id").ids, job_ids)
        with self.registry.cursor() as holder:
            self.bounded(holder)
            holder.execute("SELECT pg_backend_pid(), txid_current()")
            holder_pid, holder_txid = holder.fetchone()
            holder.execute("SELECT id FROM portfolio_import_job WHERE id IN %s ORDER BY id FOR UPDATE", [tuple(job_ids[:2])])
            self.assertEqual([row[0] for row in holder.fetchall()], job_ids[:2])
            # This independent transaction must complete BEFORE releasing the
            # holder. SQL timeouts make a blocking implementation fail boundedly.
            self.assertEqual(self.process_fresh(fixture), 5)
            with self.registry.cursor() as observer:
                env = self.environment(observer, fixture["operator_id"], fixture)
                observer.execute("SELECT pg_backend_pid(), txid_current()")
                pid, txid = observer.fetchone()
                self.assertNotEqual(pid, holder_pid)
                self.assertNotEqual(txid, holder_txid)
                self.assertEqual(env[self.JOB].browse(job_ids[:2]).mapped("state"), ["pending", "pending"])
                self.assertEqual(env[self.JOB].browse(job_ids[:2]).mapped("attempt_count"), [0, 0])
                self.assertEqual(env[self.JOB].browse(job_ids[2:]).mapped("state"), ["done"] * 5)
                self.assertEqual(env[self.ATTEMPT].search([("job_id", "in", job_ids)]).mapped("job_id").ids, job_ids[2:])
            holder.rollback()
        self.assertEqual(self.process_fresh(fixture), 2)
        self.assert_done(fixture)

    def test_retry_race_preserves_failure_and_one_explicit_retry_event(self):
        fixture = self.new_fixture()
        self.submit_jobs(fixture)
        job_id = fixture["jobs"][0]
        with self.registry.cursor() as cr:
            settings = self.environment(cr, fixture["settings_id"], fixture)
            self.assert_actor(settings, fixture, "settings")
            settings["portfolio.import.customer.map"].browse(fixture["customer_map_id"]).write({"active": False})
            cr.commit()
        self.assertEqual(self.process_fresh(fixture), 1)
        with self.registry.cursor() as cr:
            env = self.environment(cr, fixture["operator_id"], fixture)
            job = env[self.JOB].browse(job_id)
            self.assertEqual((job.state, job.attempt_count, job.error_code), ("failed", 1, "customer_mapping"))
            failure = env[self.ATTEMPT].search([("job_id", "=", job_id)])
            self.assertEqual(failure.mapped("event_type"), ["failed"])
            failure_values = failure.read()
            self.assertEqual(self.native_counts(cr, fixture)["sale_order"], 0)
        with self.registry.cursor() as cr:
            settings = self.environment(cr, fixture["settings_id"], fixture)
            self.assert_actor(settings, fixture, "settings")
            settings["portfolio.import.customer.map"].browse(fixture["customer_map_id"]).write({"active": True})
            cr.commit()
        result = self.race(fixture, retry=True)
        self.assertNotIn("second_retry_succeeded", result)
        self.assertTrue(result["second_rejected_pending"])
        with self.registry.cursor() as cr:
            env = self.environment(cr, fixture["operator_id"], fixture)
            job = env[self.JOB].browse(job_id)
            self.assertEqual((job.state, job.attempt_count), ("pending", 1))
            events = env[self.ATTEMPT].search([("job_id", "=", job_id)], order="id")
            self.assertEqual(events.mapped("event_type"), ["failed", "retry"])
            self.assertEqual(events.mapped("attempt_no"), [1, 1])
            self.assertEqual(events[:1].read(), failure_values)
            self.assertEqual(events[-1].retry_reason, "First operator repaired mapping")
            self.assertEqual(job.read(self.RECEPTION)[0], fixture["receptions"][job_id])
        self.assertEqual(self.process_fresh(fixture), 1)
        self.assert_done(fixture, attempt_count=2, event_types=["failed", "retry", "done"])
        with self.registry.cursor() as cr:
            env = self.environment(cr, fixture["integration_id"], fixture)
            self.assertEqual(env[self.ATTEMPT].browse(failure_values[0]["id"]).read(), failure_values)

    def test_actual_worker_process_exit_rolls_back_then_recovers_once(self):
        fixture = self.new_fixture()
        self.submit_jobs(fixture)
        job_id = fixture["jobs"][0]
        runtime = PROJECT / ".runtime" / "tmp"
        runtime.mkdir(parents=True, exist_ok=True)
        witness = runtime / f"worker-exit-{uuid.uuid4().hex}.json"
        config = PROJECT / ".runtime" / "odoo.conf"
        if not config.is_file():
            config = MAIN_PROJECT / ".runtime" / "odoo.conf"
        command = [
            str(PROJECT / ".venv" / "bin" / "python"),
            str(PROJECT / "addons" / "portfolio_order_bridge" / "tests" / "worker_process_probe.py"),
            "--config", str(config), "--database", "odoo_portfolio_m1_test",
            "--uid", str(fixture["operator_id"]), "--company-id", str(fixture["company_id"]),
            "--source-id", str(fixture["source_id"]), "--job-id", str(job_id),
            "--witness", str(witness),
        ]
        process = None
        try:
            process = subprocess.Popen(
                command, cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "PYTHONPYCACHEPREFIX": str(PROJECT / ".cache" / "pycache"),
                     "TMPDIR": str(runtime)},
            )
            try:
                # Captured output is deliberately discarded. Failures expose
                # only an exit classification, never native exception bodies.
                process.communicate(timeout=45)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
                self.fail("Native process-exit probe exceeded its bounded timeout")
            self.assertEqual(process.returncode, 73, f"Native process-exit probe exit code {process.returncode}")
            self.assertTrue(witness.is_file(), "Native process-exit probe did not write its checkpoint witness")
            self.assertFalse(witness.is_symlink())
            self.assertEqual(stat.S_IMODE(witness.stat().st_mode), 0o600)
            data = json.loads(witness.read_text(encoding="utf-8"))
            self.assertEqual(set(data), {
                "stage", "uid", "su", "company_id", "source_id", "job_id", "quote_id", "line_ids",
                "processing", "attempt_count", "last_attempt", "linked_quote",
            })
            self.assertEqual(data["stage"], "native_quote_flushed_before_commit")
            for name in ("uid", "company_id", "source_id", "job_id"):
                expected = fixture["operator_id"] if name == "uid" else job_id if name == "job_id" else fixture[name]
                self.assertEqual(data[name], expected)
            self.assertIs(data["su"], False)
            self.assertIs(data["processing"], True)
            self.assertIs(data["last_attempt"], True)
            self.assertIs(data["linked_quote"], False)
            self.assertEqual(data["attempt_count"], 1)
            self.assertIs(type(data["quote_id"]), int)
            self.assertGreater(data["quote_id"], 0)
            self.assertEqual(len(data["line_ids"]), 2)
            self.assertEqual(len(set(data["line_ids"])), 2)
            self.assertTrue(all(type(value) is int and value > 0 for value in data["line_ids"]))
            _logger.info("Actual worker exit code %s job %s quote %s lines %s before commit",
                         process.returncode, job_id, data["quote_id"], data["line_ids"])
            for role in ("integration", "operator"):
                with self.registry.cursor() as cr:
                    env = self.environment(cr, fixture[f"{role}_id"], fixture)
                    self.assert_actor(env, fixture, role)
                    job = env[self.JOB].browse(job_id)
                    self.assertEqual((job.state, job.attempt_count), ("pending", 0))
                    self.assertFalse(job.last_attempt_at)
                    self.assertFalse(job.sale_order_id)
                    self.assertFalse(job.error_code)
                    self.assertFalse(job.error_detail)
                    self.assertEqual(job.read(self.RECEPTION)[0], fixture["receptions"][job_id])
                    self.assertFalse(env[self.ATTEMPT].search([("job_id", "=", job_id)]))
                    self.assertEqual(self.native_counts(cr, fixture), {
                        table: 0 for table in self.native_counts(cr, fixture)
                    })
                    cr.execute("SELECT id FROM sale_order WHERE id = %s", [data["quote_id"]])
                    self.assertFalse(cr.fetchall())
                    cr.execute("SELECT id FROM sale_order_line WHERE id IN %s", [tuple(data["line_ids"])])
                    self.assertFalse(cr.fetchall())
            self.assertEqual(self.process_fresh(fixture), 1)
            quote_ids = self.assert_done(fixture)
            self.assertNotIn(data["quote_id"], quote_ids)
            self.assertEqual(self.process_fresh(fixture), 0)
            self.assertEqual(self.assert_done(fixture), quote_ids)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
            witness.unlink(missing_ok=True)
