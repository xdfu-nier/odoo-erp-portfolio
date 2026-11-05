"""Committed, separate-transaction ingress evidence in the acceptance DB only.

Each race retains its UUID-owned source and immutable pending job. Cleanup
archives sources before their actors; no business audit record is deleted.
"""

import copy
import logging
import sys
import threading
import time
import traceback
import uuid
from unittest.mock import patch

from odoo import SUPERUSER_ID, Command, api
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from psycopg2.errors import SerializationFailure

from ..payload import normalize_order_payload

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install", "portfolio_import_ingress_concurrency")
class TestImportIngressConcurrency(TransactionCase):
    JOB = "portfolio.import.job"
    SOURCE = "portfolio.import.source"
    INTEGRATION = "portfolio_order_bridge.group_import_integration"
    OPERATOR = "portfolio_order_bridge.group_import_operator"
    SALES = "sales_team.group_sale_salesman"
    FORBIDDEN = (
        "base.group_system", "base.group_erp_manager", "sales_team.group_sale_manager",
        "purchase.group_purchase_user", "stock.group_stock_user", "account.group_account_invoice",
    )
    AUDIT = [
        "source_id", "company_id", "external_order_id", "payload_hash", "raw_payload",
        "canonical_payload", "submitted_by_id", "received_at", "state", "attempt_count",
        "last_attempt_at", "error_code", "error_detail", "sale_order_id", "create_uid",
        "write_uid", "create_date", "write_date",
    ]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # This guard precedes every cursor capable of committing a fixture.
        if cls.cr.dbname != "odoo_portfolio_m1_test":
            raise AssertionError("Committed concurrency fixtures require odoo_portfolio_m1_test")
        cls.source_ids = []
        cls.actor_ids = []
        cls.companies = []
        with cls.registry.cursor() as cr:
            admin = api.Environment(cr, SUPERUSER_ID, {
                "disable_company_pricelist_creation": True, "tracking_disable": True,
            })
            employee_group = admin.ref("base.group_user")
            feature_groups = (
                admin.ref("base.group_multi_currency") | admin.ref("product.group_product_pricelist")
            )
            features_before = {
                group.id: (group in employee_group.implied_ids,
                           sorted(group.with_context(active_test=False).users.ids))
                for group in feature_groups
            }
            pricelists_before = admin["product.pricelist"].with_context(active_test=False).search([]).ids
            cny = admin.ref("base.CNY")
            cny.active = True
            for suffix in ("a", "b"):
                company = admin["res.company"].create({
                    "name": f"Ingress RR {suffix} {uuid.uuid4().hex[:12]}", "currency_id": cny.id,
                })
                cls.companies.append({"id": company.id, "suffix": suffix})
            # Native currency activation must not alter existing employee
            # permissions or create global/default company pricelists.
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
                    raise AssertionError("Ingress fixture must preserve preexisting currency feature groups")
            if admin["product.pricelist"].with_context(active_test=False).search([]).ids != pricelists_before:
                raise AssertionError("Ingress fixture must preserve preexisting company pricelist defaults")
            cls.company_ids = [company["id"] for company in cls.companies]
            admin = admin(context={
                "allowed_company_ids": cls.company_ids, "tracking_disable": True,
                "disable_company_pricelist_creation": True,
            })

            def make_user(role, groups, company_id, company_ids=None):
                user = new_test_user(
                    admin, login=f"ingress_rr_{role}_{uuid.uuid4().hex}", groups=groups,
                    company_id=company_id, company_ids=[Command.set(company_ids or [company_id])],
                    context={"no_reset_password": True},
                )
                cls.actor_ids.append(user.id)
                return user.id

            for company in cls.companies:
                company_id, suffix = company["id"], company["suffix"]
                warehouse = admin["stock.warehouse"].search([("company_id", "=", company_id)], limit=1)
                if not warehouse:
                    warehouse = admin["stock.warehouse"].create({
                        "name": f"Ingress RR warehouse {uuid.uuid4().hex[:12]}",
                        "code": f"I{uuid.uuid4().hex[:4]}", "company_id": company_id,
                    })
                company["warehouse_id"] = warehouse.id
                company["pricelist_id"] = admin["product.pricelist"].create({
                    "name": f"Ingress RR CNY {uuid.uuid4().hex[:12]}",
                    "currency_id": cny.id, "company_id": company_id,
                }).id
                company["sales_id"] = make_user(f"sales_{suffix}", cls.SALES, company_id)
                company["operator_id"] = make_user(
                    f"operator_{suffix}", f"{cls.OPERATOR},{cls.SALES}", company_id,
                )
                company["integration_id"] = make_user(f"integration_{suffix}", cls.INTEGRATION, company_id)
            cls.company_a, cls.company_b = cls.companies
            cls.other_integration_id = make_user("other_integration", cls.INTEGRATION, cls.company_a["id"])
            cls.integration_both_id = make_user(
                "integration_both", cls.INTEGRATION, cls.company_a["id"], cls.company_ids,
            )
            cls.settings_id = make_user(
                "settings", "base.group_system", cls.company_a["id"], cls.company_ids,
            )
            cr.commit()
        cls.addClassCleanup(cls._archive_fixtures)
        _logger.info("Ingress RR fixtures preserved preexisting currency feature groups and pricelist defaults")

    @classmethod
    def _archive_fixtures(cls):
        with cls.registry.cursor() as cr:
            settings = api.Environment(cr, cls.settings_id, {"allowed_company_ids": cls.company_ids})
            if settings.su or settings.user.share:
                raise AssertionError("Source cleanup requires the ordinary internal Settings actor")
            # Configuration validation still requires active configured users
            # even while archiving a source, so actors must be archived last.
            settings[cls.SOURCE].with_context(active_test=False).search([
                ("id", "in", cls.source_ids),
            ]).write({"active": False})
            admin = api.Environment(cr, SUPERUSER_ID, {"allowed_company_ids": cls.company_ids})
            admin["res.users"].browse(cls.actor_ids).write({"active": False})
            cr.commit()

    def environment(self, cr, uid, company_ids):
        # The post-install runner holds Registry._lock. Each new transaction
        # uses its already loaded registry rather than loading it in a worker.
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, uid, {
            "allowed_company_ids": list(company_ids), "tracking_disable": True,
            "mail_create_nosubscribe": True, "lang": "en_US",
        })

    def assert_integration(self, env, uid, company_ids):
        self.assertEqual(env.uid, uid)
        self.assertNotEqual(env.uid, SUPERUSER_ID)
        self.assertFalse(env.su)
        self.assertTrue(env.user.active)
        self.assertFalse(env.user.share)
        self.assertTrue(env.user.has_group(self.INTEGRATION))
        self.assertEqual(set(env.companies.ids), set(company_ids))
        self.assertEqual(set(env.user.company_ids.ids), set(company_ids))
        for group in (*self.FORBIDDEN, self.SALES, self.OPERATOR):
            self.assertFalse(env.user.has_group(group), group)

    def new_source(self, company=None, external_order_id=None):
        company = company or self.company_a
        with self.registry.cursor() as cr:
            env = self.environment(cr, self.settings_id, [company["id"]])
            self.assertFalse(env.su)
            self.assertNotEqual(env.uid, SUPERUSER_ID)
            self.assertFalse(env.user.share)
            self.assertTrue(env.user.has_group("base.group_system"))
            for role in ("sales", "operator"):
                actor = env["res.users"].browse(company[f"{role}_id"])
                self.assertTrue(actor.active)
                self.assertFalse(actor.share)
                self.assertEqual(actor.company_ids.ids, [company["id"]])
                self.assertTrue(actor.has_group(self.SALES))
            operator = env["res.users"].browse(company["operator_id"])
            self.assertTrue(operator.has_group(self.OPERATOR))
            self.assertNotEqual(operator.id, company["sales_id"])
            for group in self.FORBIDDEN:
                self.assertFalse(operator.has_group(group), group)
            integrations = [company["integration_id"], self.integration_both_id]
            if company == self.company_a:
                integrations.append(self.other_integration_id)
            source = env[self.SOURCE].create({
                "name": f"Ingress RR source {uuid.uuid4().hex}",
                "company_id": company["id"], "company_code": f"RR-C-{uuid.uuid4().hex}",
                "code": f"RR-S-{uuid.uuid4().hex}", "pricelist_id": company["pricelist_id"],
                "warehouse_id": company["warehouse_id"], "sales_user_id": company["sales_id"],
                "processor_user_id": company["operator_id"],
                "integration_user_ids": [Command.set(integrations)],
            })
            self.assertEqual(source.ingress_revision, 0)
            fixture = {
                "source_id": source.id, "company_id": company["id"],
                "company_code": source.company_code, "source_code": source.code,
                "external_order_id": external_order_id or f"RR-O-{uuid.uuid4().hex}",
            }
            cr.commit()
        self.source_ids.append(fixture["source_id"])
        return fixture

    def payload(self, fixture):
        return {
            "company_code": fixture["company_code"], "source_code": fixture["source_code"],
            "external_order_id": fixture["external_order_id"], "customer_ref": "UNMAPPED-CUSTOMER",
            "currency": "CNY", "request_id": "original-request", "sent_at": "2026-11-03T08:00:00Z",
            "lines": [
                {"external_line_id": "2", "sku": "UNMAPPED-SKU-2", "quantity": "1.5",
                 "unit_price": "12.50", "discount": "1.00"},
                {"external_line_id": "1", "sku": "UNMAPPED-SKU-1", "quantity": "2",
                 "unit_price": "20", "discount": "0"},
            ],
        }

    def bounded_transaction(self, cr):
        cr.execute("SELECT set_config('lock_timeout', '15000', true), set_config('statement_timeout', '20000', true)")

    def source_revision(self, cr, fixture):
        # Revision is a configuration field: read it as the ordinary Settings
        # actor in this same transaction without elevating the submitter.
        settings = self.environment(cr, self.settings_id, [fixture["company_id"]])
        return settings[self.SOURCE].browse(fixture["source_id"]).ingress_revision

    def transaction_snapshot(self, env, fixture):
        source = env[self.SOURCE].browse(fixture["source_id"])
        source.read(["company_id", "company_code", "code"])
        self.assertEqual(source.company_id.id, fixture["company_id"])
        revision = self.source_revision(env.cr, fixture)
        env.cr.execute("SELECT pg_backend_pid(), txid_current(), current_setting('transaction_isolation')")
        pid, txid, isolation = env.cr.fetchone()
        self.assertEqual(isolation, "repeatable read")
        return {"pid": pid, "txid": txid, "isolation": isolation, "revision": revision}

    def sale_order_count(self, env, company_id):
        # Read-only, company-bounded side-effect witness. Integration actors
        # intentionally have no sale.order ACL and receive no Sales privilege.
        env.cr.execute("SELECT count(*) FROM sale_order WHERE company_id = %s", [company_id])
        return env.cr.fetchone()[0]

    def race(self, fixture, original, contender):
        result = {"retries": 0, "attempts": [], "serialization_codes": []}
        preloaded = threading.Event()
        enter_submit = threading.Event()
        stop = threading.Event()
        company_ids = [fixture["company_id"]]
        first_uid = self.company_a["integration_id"]
        second_uid = self.other_integration_id
        with self.registry.cursor() as first_cr:
            self.bounded_transaction(first_cr)
            first_env = self.environment(first_cr, first_uid, company_ids)
            self.assert_integration(first_env, first_uid, company_ids)
            result["first_snapshot"] = self.transaction_snapshot(first_env, fixture)
            result["sale_orders_before"] = self.sale_order_count(first_env, fixture["company_id"])
            self.assertEqual(result["sale_orders_before"], 0)
            result["first"] = first_env[self.JOB].submit_order(original)
            result["original_audit"] = first_env[self.JOB].browse(result["first"]["job_id"]).read(self.AUDIT)[0]
            self.assertEqual(self.source_revision(first_cr, fixture), 1)

            def worker():
                try:
                    for attempt in range(5):
                        if stop.is_set():
                            return
                        # A genuinely new cursor/transaction on EVERY attempt;
                        # PostgreSQL cannot repair a repeatable-read snapshot.
                        with self.registry.cursor() as second_cr:
                            self.bounded_transaction(second_cr)
                            second_env = self.environment(second_cr, second_uid, company_ids)
                            self.assert_integration(second_env, second_uid, company_ids)
                            result["attempts"].append(self.transaction_snapshot(second_env, fixture))
                            if attempt == 0:
                                preloaded.set()
                                if not enter_submit.wait(10):
                                    raise AssertionError("The stale ingress contender was not released")
                            if stop.is_set():
                                return
                            try:
                                response = second_env[self.JOB].submit_order(contender)
                                if stop.is_set():
                                    second_cr.rollback()
                                    return
                                second_cr.commit()
                                result["second"] = response
                                return
                            except SerializationFailure as error:
                                second_cr.rollback()
                                result["retries"] += 1
                                result["serialization_codes"].append(error.pgcode)
                                _logger.info("Ingress RR source %s SQLSTATE %s retry %s in a new cursor",
                                             fixture["source_id"], error.pgcode, attempt + 1)
                            except UserError as error:
                                if type(error) is not UserError:
                                    raise
                                second_cr.rollback()
                                result["blocked"] = str(error)
                                return
                    raise AssertionError("Ingress race exhausted fresh-transaction serialization retries")
                except BaseException as error:
                    result["error"] = error
                    preloaded.set()

            thread = threading.Thread(target=worker, daemon=True, name=f"ingress-rr-{fixture['source_id']}")
            thread.start()
            committed = False
            try:
                if not preloaded.wait(10):
                    frame = sys._current_frames().get(thread.ident)
                    self.fail("Ingress contender did not preload: " + "".join(traceback.format_stack(frame)))
                if "error" in result:
                    raise result["error"]
                stale = result["attempts"][0]
                self.assertEqual(stale["revision"], 0)
                self.assertNotEqual(stale["pid"], result["first_snapshot"]["pid"])
                self.assertNotEqual(stale["txid"], result["first_snapshot"]["txid"])
                enter_submit.set()
                deadline = time.monotonic() + 10
                with self.registry.cursor() as observer:
                    observer.execute("SELECT pg_backend_pid()")
                    result["observer_pid"] = observer.fetchone()[0]
                    self.assertNotIn(result["observer_pid"], [stale["pid"], result["first_snapshot"]["pid"]])
                    while time.monotonic() < deadline:
                        observer.execute("SELECT pg_stat_clear_snapshot()")
                        observer.execute(
                            "SELECT wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid = %s",
                            [stale["pid"]],
                        )
                        row = observer.fetchone()
                        if row and row[0] == "Lock" and result["first_snapshot"]["pid"] in row[1]:
                            result["observed_lock_wait"] = True
                            _logger.info("Ingress RR source %s observer PID %s saw second PID %s blocked by first PID %s before commit",
                                         fixture["source_id"], result["observer_pid"], stale["pid"], result["first_snapshot"]["pid"])
                            break
                        if not thread.is_alive():
                            break
                        stop.wait(0.01)
                if "error" in result:
                    raise result["error"]
                self.assertTrue(result.get("observed_lock_wait"), f"No native ingress lock wait: {result}")
                first_cr.commit()
                committed = True
            finally:
                if not committed:
                    stop.set()
                enter_submit.set()
                first_cr.rollback()
                thread.join(25)
                self.assertFalse(thread.is_alive(), "Ingress contender failed bounded cleanup")
        if "error" in result:
            raise result["error"]
        self.assertTrue(result["observed_lock_wait"])
        self.assertGreaterEqual(result["retries"], 1, result)
        self.assertEqual(result["serialization_codes"], ["40001"] * result["retries"])
        self.assertGreaterEqual(len(result["attempts"]), 2, result)
        self.assertEqual(result["attempts"][1]["revision"], 1)
        self.assertEqual(len({attempt["txid"] for attempt in result["attempts"]}), len(result["attempts"]))
        _logger.info("Ingress RR source %s job %s retries=%s old_txid=%s fresh_txid=%s reused=%s rejected=%s",
                     fixture["source_id"], result["first"]["job_id"], result["retries"],
                     result["attempts"][0]["txid"], result["attempts"][1]["txid"],
                     result.get("second", {}).get("reused"), result.get("blocked"))
        return result

    def assert_pending_job(self, env, fixture, response, payload, submitted_by):
        jobs = env[self.JOB].search([
            ("source_id", "=", fixture["source_id"]), ("company_id", "=", fixture["company_id"]),
            ("external_order_id", "=", fixture["external_order_id"]),
        ])
        self.assertEqual(jobs.ids, [response["job_id"]])
        normalized = normalize_order_payload(payload)
        self.assertEqual(jobs.source_id.id, fixture["source_id"])
        self.assertEqual(jobs.company_id.id, fixture["company_id"])
        self.assertEqual(jobs.external_order_id, fixture["external_order_id"])
        self.assertEqual(jobs.payload_hash, normalized.payload_hash)
        self.assertEqual(jobs.raw_payload, normalized.raw_json)
        self.assertEqual(jobs.canonical_payload, normalized.canonical_json)
        self.assertEqual(jobs.submitted_by_id.id, submitted_by)
        self.assertEqual(jobs.create_uid.id, submitted_by)
        self.assertTrue(jobs.received_at)
        self.assertEqual(jobs.state, "pending")
        self.assertEqual(jobs.attempt_count, 0)
        for field in ("last_attempt_at", "error_code", "error_detail", "sale_order_id"):
            self.assertFalse(jobs[field], field)
        self.assertEqual(response, {
            "job_id": jobs.id, "state": "pending", "sale_order_id": False, "reused": False,
        })
        return jobs

    def assert_race_audit(self, fixture, original, result, revision):
        # Neither the runner's old transaction nor a contender's cache can be
        # used as evidence of what was actually committed.
        for uid in (self.company_a["integration_id"], self.other_integration_id):
            with self.registry.cursor() as cr:
                env = self.environment(cr, uid, [fixture["company_id"]])
                self.assert_integration(env, uid, [fixture["company_id"]])
                jobs = self.assert_pending_job(
                    env, fixture, result["first"], original, self.company_a["integration_id"],
                )
                self.assertEqual(jobs.read(self.AUDIT)[0], result["original_audit"])
                self.assertEqual(env[self.JOB].search_count([("source_id", "=", fixture["source_id"])]), 1)
                self.assertEqual(self.source_revision(cr, fixture), revision)
                self.assertEqual(self.sale_order_count(env, fixture["company_id"]), result["sale_orders_before"])

    def test_same_payload_waits_retries_and_reuses_one_original_job(self):
        fixture = self.new_source()
        original = self.payload(fixture)
        result = self.race(fixture, original, copy.deepcopy(original))
        self.assertNotIn("blocked", result)
        self.assertEqual(result["second"], {**result["first"], "reused": True})
        self.assert_race_audit(fixture, original, result, revision=2)

    def test_changed_quantity_or_price_retries_then_conflicts_without_overwrite(self):
        for field, value in (("quantity", "3"), ("unit_price", "12.51")):
            with self.subTest(field=field):
                fixture = self.new_source()
                original = self.payload(fixture)
                contender = copy.deepcopy(original)
                contender["lines"][0][field] = value
                self.assertNotEqual(normalize_order_payload(original).payload_hash,
                                    normalize_order_payload(contender).payload_hash)
                result = self.race(fixture, original, contender)
                self.assertNotIn("second", result)
                self.assertEqual(result["blocked"], "order_conflict: external_order_id")
                self.assert_race_audit(fixture, original, result, revision=1)

    def test_equivalent_lines_decimals_and_transport_reuse_original_raw_after_retry(self):
        fixture = self.new_source()
        original = self.payload(fixture)
        contender = copy.deepcopy(original)
        contender.update(request_id="different-request", sent_at="2026-11-03T09:00:00Z")
        contender["lines"].reverse()
        contender["lines"][0].update(quantity="0002.000000", unit_price="020.000", discount="0.000")
        contender["lines"][1].update(quantity="01.500", unit_price="012.500000", discount="01")
        self.assertEqual(normalize_order_payload(original).payload_hash,
                         normalize_order_payload(contender).payload_hash)
        self.assertNotEqual(normalize_order_payload(original).raw_json,
                            normalize_order_payload(contender).raw_json)
        result = self.race(fixture, original, contender)
        self.assertNotIn("blocked", result)
        self.assertEqual(result["second"], {**result["first"], "reused": True})
        self.assert_race_audit(fixture, original, result, revision=2)

    def test_ingress_insert_and_processing_do_not_deadlock_on_company_source(self):
        fixture = self.new_source(self.company_b)
        original = self.payload(fixture)
        incoming = copy.deepcopy(original)
        incoming["external_order_id"] = f"RR-NEW-{uuid.uuid4().hex}"
        incoming["request_id"] = "concurrent-new-reception"
        company_ids = [fixture["company_id"]]
        integration_uid = self.company_b["integration_id"]
        operator_uid = self.company_b["operator_id"]
        with self.registry.cursor() as cr:
            env = self.environment(cr, integration_uid, company_ids)
            self.assert_integration(env, integration_uid, company_ids)
            first = env[self.JOB].submit_order(original)
            first_job = self.assert_pending_job(env, fixture, first, original, integration_uid)
            original_audit = first_job.read([
                "raw_payload", "canonical_payload", "payload_hash", "submitted_by_id", "received_at",
            ])[0]
            sale_orders_before = self.sale_order_count(env, fixture["company_id"])
            cr.commit()

        result = {"worker_attempts": [], "serialization_codes": [], "errors": {}}
        ingress_paused = threading.Event()
        worker_preloaded = threading.Event()
        release_ingress = threading.Event()
        stop = threading.Event()
        job_class = type(self.env[self.JOB])
        real_configuration = job_class._ingress_configuration

        def pause_after_real_configuration(records, source, data):
            value = real_configuration(records, source, data)
            if records.env.uid == integration_uid and source.id == fixture["source_id"]:
                # All actual authorization, source UPDATE and configuration
                # checks have run. The next ordinary operation inserts a job
                # with native source/company foreign keys in this transaction.
                result["ingress_snapshot"] = self.transaction_snapshot(records.env, fixture)
                ingress_paused.set()
                if not release_ingress.wait(12):
                    raise AssertionError("The concurrent ingress insert was not released")
            return value

        def ingress_thread():
            try:
                with self.registry.cursor() as cr:
                    self.bounded_transaction(cr)
                    env = self.environment(cr, integration_uid, company_ids)
                    self.assert_integration(env, integration_uid, company_ids)
                    response = env[self.JOB].submit_order(incoming)
                    if stop.is_set():
                        cr.rollback()
                        return
                    cr.commit()
                    result["incoming"] = response
            except BaseException as error:
                result["errors"]["ingress"] = error
                ingress_paused.set()

        def processing_thread():
            try:
                for attempt in range(5):
                    if stop.is_set():
                        return
                    with self.registry.cursor() as cr:
                        self.bounded_transaction(cr)
                        env = self.environment(cr, operator_uid, company_ids)
                        self.assertEqual(env.uid, operator_uid)
                        self.assertFalse(env.su)
                        self.assertTrue(env.user.active)
                        self.assertFalse(env.user.share)
                        self.assertTrue(env.user.has_group(self.OPERATOR))
                        self.assertTrue(env.user.has_group(self.SALES))
                        self.assertEqual(env.user.company_ids.ids, company_ids)
                        self.assertEqual(env.companies.ids, company_ids)
                        for group in (*self.FORBIDDEN, "sales_team.group_sale_salesman_all_leads"):
                            self.assertFalse(env.user.has_group(group), group)
                        result["worker_attempts"].append(self.transaction_snapshot(env, fixture))
                        worker_preloaded.set()
                        try:
                            env[self.JOB]._process_pending()
                            if stop.is_set():
                                cr.rollback()
                                return
                            cr.commit()
                            result["worker_committed"] = True
                            return
                        except SerializationFailure as error:
                            cr.rollback()
                            result["serialization_codes"].append(error.pgcode)
                            _logger.info("Ingress/worker source %s SQLSTATE %s fresh worker retry %s",
                                         fixture["source_id"], error.pgcode, attempt + 1)
                raise AssertionError("The ingress/worker race exhausted fresh worker transactions")
            except BaseException as error:
                # A native 40P01 is deliberately terminal evidence, never
                # swallowed by the allowed repeatable-read 40001 retry path.
                result["errors"]["worker"] = error
                worker_preloaded.set()

        threads = [
            threading.Thread(target=ingress_thread, daemon=True,
                             name=f"new-ingress-{fixture['source_id']}"),
            threading.Thread(target=processing_thread, daemon=True,
                             name=f"processing-ingress-{fixture['source_id']}"),
        ]
        with patch.object(job_class, "_ingress_configuration", pause_after_real_configuration):
            try:
                threads[0].start()
                self.assertTrue(ingress_paused.wait(10), "Ingress did not reach its real pre-insert boundary")
                if result["errors"]:
                    raise next(iter(result["errors"].values()))
                self.assertEqual(result["ingress_snapshot"]["revision"], 2)
                threads[1].start()
                self.assertTrue(worker_preloaded.wait(10), "The ordinary worker transaction did not preload")
                if result["errors"]:
                    raise next(iter(result["errors"].values()))
                worker = result["worker_attempts"][0]
                ingress = result["ingress_snapshot"]
                self.assertEqual(worker["revision"], 1)
                self.assertNotEqual(worker["pid"], ingress["pid"])
                self.assertNotEqual(worker["txid"], ingress["txid"])
                deadline = time.monotonic() + 10
                with self.registry.cursor() as observer:
                    observer.execute("SELECT pg_backend_pid()")
                    observer_pid = observer.fetchone()[0]
                    self.assertNotIn(observer_pid, [worker["pid"], ingress["pid"]])
                    while time.monotonic() < deadline:
                        observer.execute("SELECT pg_stat_clear_snapshot()")
                        observer.execute(
                            "SELECT wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid = %s",
                            [worker["pid"]],
                        )
                        row = observer.fetchone()
                        if row and row[0] == "Lock" and ingress["pid"] in row[1]:
                            result["observed_lock_wait"] = True
                            _logger.info("Ingress/worker source %s observer PID %s saw worker PID %s blocked by ingress PID %s before new-job insert",
                                         fixture["source_id"], observer_pid, worker["pid"], ingress["pid"])
                            break
                        if not threads[1].is_alive():
                            break
                        stop.wait(0.01)
                self.assertTrue(result.get("observed_lock_wait"), f"No native ingress/worker wait: {result}")
                release_ingress.set()
                for thread in threads:
                    thread.join(25)
                self.assertTrue(all(not thread.is_alive() for thread in threads),
                                "Ingress/worker transactions did not finish within bounded time")
            finally:
                stop.set()
                release_ingress.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(25)
                self.assertTrue(all(not thread.is_alive() for thread in threads),
                                "Ingress/worker transactions failed bounded cleanup")
        error_codes = {name: getattr(error, "pgcode", None) for name, error in result["errors"].items()}
        self.assertNotIn("40P01", error_codes.values(), f"Native company/source FK deadlock: {error_codes}")
        if result["errors"]:
            raise next(iter(result["errors"].values()))
        self.assertTrue(result.get("worker_committed"), result)
        self.assertIn("incoming", result)
        self.assertEqual(result["serialization_codes"], ["40001"] * len(result["serialization_codes"]))
        self.assertEqual(len({item["txid"] for item in result["worker_attempts"]}),
                         len(result["worker_attempts"]))
        with self.registry.cursor() as cr:
            env = self.environment(cr, integration_uid, company_ids)
            jobs = env[self.JOB].search([("source_id", "=", fixture["source_id"])], order="id")
            self.assertEqual(jobs.ids, [first["job_id"], result["incoming"]["job_id"]])
            self.assertEqual(jobs.mapped("state"), ["failed", "failed"])
            self.assertEqual(jobs.mapped("attempt_count"), [1, 1])
            self.assertFalse(jobs.sale_order_id)
            self.assertEqual(jobs[:1].read(list(original_audit.keys() - {"id"}))[0], original_audit)
            normalized = normalize_order_payload(incoming)
            self.assertEqual(jobs[-1].raw_payload, normalized.raw_json)
            self.assertEqual(jobs[-1].canonical_payload, normalized.canonical_json)
            self.assertEqual(jobs[-1].payload_hash, normalized.payload_hash)
            self.assertEqual(jobs[-1].submitted_by_id.id, integration_uid)
            self.assertEqual(self.sale_order_count(env, fixture["company_id"]), sale_orders_before)
            events = env["portfolio.import.attempt"].search([("job_id", "in", jobs.ids)], order="job_id")
            self.assertEqual(events.mapped("job_id").ids, jobs.ids)
            self.assertEqual(events.mapped("event_type"), ["failed", "failed"])
            self.assertEqual(events.mapped("actor_id").ids, [operator_uid])

    def test_same_external_identity_is_separate_for_owned_sources_and_companies(self):
        external_order_id = f"RR-SHARED-{uuid.uuid4().hex}"
        fixtures = [self.new_source(company, external_order_id) for company in (
            self.company_a, self.company_a, self.company_b,
        )]
        responses = []
        transactions = []
        for fixture in fixtures:
            with self.registry.cursor() as cr:
                env = self.environment(cr, self.integration_both_id, self.company_ids)
                self.assert_integration(env, self.integration_both_id, self.company_ids)
                self.assertEqual(set(env.user.company_ids.ids), set(self.company_ids))
                transactions.append(self.transaction_snapshot(env, fixture)["txid"])
                responses.append(env[self.JOB].submit_order(self.payload(fixture)))
                cr.commit()
        self.assertEqual(len(set(transactions)), 3)
        self.assertEqual(len({response["job_id"] for response in responses}), 3)
        with self.registry.cursor() as cr:
            env = self.environment(cr, self.integration_both_id, self.company_ids)
            self.assert_integration(env, self.integration_both_id, self.company_ids)
            self.assertEqual(set(env[self.JOB].search([
                ("external_order_id", "=", external_order_id),
                ("source_id", "in", [fixture["source_id"] for fixture in fixtures]),
            ]).ids), {response["job_id"] for response in responses})
            for fixture, response in zip(fixtures, responses, strict=True):
                self.assert_pending_job(env, fixture, response, self.payload(fixture), self.integration_both_id)
                self.assertEqual(self.source_revision(cr, fixture), 1)
                self.assertEqual(self.sale_order_count(env, fixture["company_id"]), 0)
        # Each company's ordinary single-company integration user sees only
        # its own authorized identities in another fresh transaction.
        for company, expected in ((self.company_a, responses[:2]), (self.company_b, responses[2:])):
            with self.registry.cursor() as cr:
                env = self.environment(cr, company["integration_id"], [company["id"]])
                self.assert_integration(env, company["integration_id"], [company["id"]])
                self.assertEqual(env.user.company_ids.ids, [company["id"]])
                self.assertEqual(set(env[self.JOB].search([
                    ("external_order_id", "=", external_order_id),
                ]).ids), {response["job_id"] for response in expected})
