"""Native quotation worker boundaries, exact commercial values and atomic outcomes."""

import json
import unittest
from contextlib import closing
from decimal import Decimal
from unittest.mock import patch

from odoo import Command, api, fields
from odoo.exceptions import AccessError, UserError
from odoo.service.model import get_public_method
from odoo.tests import tagged
from psycopg2 import errors

from .common import ImportWorkerCase


@tagged("post_install", "-at_install", "portfolio_import_worker")
class TestImportWorker(ImportWorkerCase):
    def test_native_draft_quotation_preserves_values_and_original_actor(self):
        before = self.native_counts()
        started = fields.Datetime.now()
        payload = self.payload()
        payload["lines"][0]["discount"] = "15"
        response = self.submit(payload)
        job = self.job(response, self.operator_a)
        self.process()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.attempt_count, 1)
        self.assertGreaterEqual(job.last_attempt_at, started)
        self.assertEqual(job.submitted_by_id, self.integration_a)
        quote = job.sale_order_id
        self.assertTrue(quote)
        self.assertEqual(quote.state, "draft")
        self.assertEqual(quote.create_uid, self.operator_a)
        self.assertEqual(quote.portfolio_creator_id, self.operator_a)
        self.assertEqual(quote.user_id, self.sales_a)
        self.assertEqual(quote.company_id, self.company_a)
        self.assertEqual(quote.partner_id, self.customer)
        self.assertEqual(quote.pricelist_id, self.pricelist_a)
        self.assertEqual(quote.warehouse_id, self.warehouse_a)
        self.assertEqual(quote.currency_id, self.cny)
        self.assertIn(self.source_a.code, quote.client_order_ref)
        self.assertIn("ORDER-1", quote.client_order_ref)
        self.assertIn(self.source_a.code, quote.origin)
        self.assertIn("ORDER-1", quote.origin)
        lines = quote.order_line.sorted(key=lambda line: line.product_id.id)
        self.assertEqual(lines.product_id, self.products)
        self.assertEqual(lines.mapped("product_uom"), self.unit)
        self.assertEqual(lines.mapped("product_uom_qty"), [2.0, 1.5])
        self.assertEqual(lines.mapped("price_unit"), [20.0, 12.5])
        self.assertEqual(lines.mapped("discount"), [0.0, 15.0])
        self.assertFalse(lines.tax_id)
        self.assertEqual(Decimal(str(quote.amount_untaxed)), Decimal("55.94"))
        self.assertEqual(quote.amount_total, quote.amount_untaxed)
        after = self.native_counts()
        self.assertEqual(after["sale.order"], before["sale.order"] + 1)
        self.assertEqual(after["sale.order.line"], before["sale.order.line"] + 2)
        for model in ("stock.picking", "stock.move", "purchase.order", "account.move"):
            self.assertEqual(after[model], before[model])
        attempts = self.attempts(job)
        self.assertEqual(len(attempts), 1)
        self.assertEqual((attempts.event_type, attempts.attempt_no), ("done", 1))
        self.assertEqual(attempts.actor_id, self.operator_a)
        self.assertEqual(attempts.job_id, job)
        self.assertEqual(attempts.company_id, self.company_a)
        self.assertEqual(attempts.started_at, job.last_attempt_at)
        self.assertGreaterEqual(attempts.finished_at, attempts.started_at)
        snapshot = json.loads(attempts.mapping_snapshot)
        self.assertEqual(snapshot["customer"]["mapping_id"], self.customer_map.id)
        self.assertEqual(snapshot["customer"]["partner_id"], self.customer.id)
        self.assertEqual({item["mapping_id"] for item in snapshot["lines"]}, set(self.product_maps.ids))
        self.assertEqual({item["product_id"] for item in snapshot["lines"]}, set(self.products.ids))
        self.assertEqual({item["uom_id"] for item in snapshot["lines"]}, {self.unit.id})
        defaults = json.loads(attempts.source_defaults_snapshot)
        self.assertEqual(defaults, {
            "source_id": self.source_a.id, "company_id": self.company_a.id,
            "warehouse_id": self.warehouse_a.id, "pricelist_id": self.pricelist_a.id,
            "sales_user_id": self.sales_a.id, "processor_user_id": self.operator_a.id,
        })
        self.process()
        self.assertEqual(self.native_counts(), after)
        self.assertEqual(self.attempts(job).ids, attempts.ids)
        self.assertEqual(self.submit(payload), {
            "job_id": job.id, "state": "done", "sale_order_id": quote.id, "reused": True,
        })
        with self.assertRaisesRegex(UserError, "approval"):
            quote.with_user(self.sales_a).action_confirm()

    def test_batch_is_bounded_to_five_eligible_pending_jobs(self):
        jobs = [self.job(self.submit(self.payload(external_order_id=f"BATCH-{index}")),
                         self.operator_a) for index in range(7)]
        foreign = self.submit(self.payload(self.source_b), user=self.integration_b)
        other = self.submit(self.payload(self.other_source), user=self.other_integration)
        self.process()
        self.assertEqual([job.state for job in jobs], ["done"] * 5 + ["pending"] * 2)
        self.assertEqual(self.job(foreign, self.operator_b).state, "pending")
        self.assertEqual(self.job(other, self.other_operator).state, "pending")
        self.process()
        self.assertEqual([job.state for job in jobs], ["done"] * 7)

    def test_missing_customer_or_product_mapping_fails_without_creating_masters(self):
        for model, record in (("customer", self.customer_map), ("product", self.product_maps[:1])):
            with self.subTest(mapping=model), closing(self.cr.savepoint()):
                record.write({"active": False})
                before = self.native_counts()
                masters = {name: self.native[name].search_count([])
                           for name in ("res.partner", "product.product")}
                job = self.job(self.submit(), self.operator_a)
                self.process()
                self.assertEqual(job.state, "failed")
                self.assertEqual(job.attempt_count, 1)
                self.assertFalse(job.sale_order_id)
                self.assertTrue(job.error_code)
                self.assertEqual(self.native_counts(), before)
                self.assertEqual({name: self.native[name].search_count([]) for name in masters}, masters)
                event = self.attempts(job)
                self.assertEqual((event.event_type, event.attempt_no), ("failed", 1))
                self.assertTrue(event.error_path)
                self.assertTrue(event.error_detail)
                self.process()
                self.assertEqual(self.attempts(job).ids, event.ids)

    def test_changed_native_targets_fail_closed(self):
        changes = (
            (self.customer, {"active": False}),
            (self.customer, {"company_id": self.company_b.id}),
            (self.products[:1], {"active": False}),
            (self.products[:1].product_tmpl_id, {"active": False}),
            (self.products[:1], {"company_id": self.company_b.id}),
            (self.unit, {"active": False}),
        )
        for record, values in changes:
            with self.subTest(model=record._name, values=values), closing(self.cr.savepoint()):
                response = self.submit()
                record.write(values)
                before = self.native_counts()
                self.process()
                job = self.job(response, self.operator_a)
                self.assertEqual(job.state, "failed")
                self.assertFalse(job.sale_order_id)
                self.assertEqual(self.native_counts(), before)
                self.assertEqual(self.attempts(job).event_type, "failed")

    def test_inactive_commercial_parent_is_rejected(self):
        parent = self.native["res.partner"].create({"name": "Worker parent", "ref": "WORKER-PARENT"})
        self.customer.parent_id = parent
        response = self.submit()
        parent.active = False
        self.process()
        self.assertEqual(self.job(response, self.operator_a).state, "failed")

    def test_ambiguous_native_customer_ref_and_product_sku_are_rejected(self):
        for model, values in (
            ("res.partner", {"name": "Duplicate native ref", "ref": self.customer.ref}),
            ("product.product", {"name": "Duplicate native SKU",
                                 "default_code": self.products[:1].default_code}),
        ):
            with self.subTest(model=model), closing(self.cr.savepoint()):
                response = self.submit()
                self.native[model].create(values)
                before = self.native_counts()
                self.process()
                self.assertEqual(self.job(response, self.operator_a).state, "failed")
                self.assertEqual(self.native_counts(), before)

    def test_quantity_must_fit_current_product_base_unit_grain(self):
        payload = self.payload()
        payload["lines"][0]["quantity"] = "1.25"
        job = self.job(self.submit(payload), self.operator_a)
        before = self.native_counts()
        self.process()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "quantity_precision")
        self.assertEqual(self.attempts(job).error_path, "lines[0].quantity")
        self.assertEqual(self.native_counts(), before)

    def test_current_currency_and_discount_precision_are_revalidated(self):
        precision = self.env["decimal.precision"].search([("name", "=", "Discount")], limit=1)
        precision.digits = 3
        payload = self.payload()
        payload["lines"][0]["discount"] = "1.001"
        response = self.submit(payload)
        precision.digits = 2
        self.process()
        job = self.job(response, self.operator_a)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error_code, "discount_precision")
        self.assertEqual(self.attempts(job).error_path, "lines[0].discount")

    def test_current_pricelist_currency_and_source_defaults_are_revalidated(self):
        for record, values in (
            (self.pricelist_a, {"currency_id": self.usd.id}),
            (self.pricelist_a, {"active": False}),
            (self.warehouse_a, {"active": False}),
            (self.sales_a, {"active": False}),
        ):
            with self.subTest(model=record._name, values=values), closing(self.cr.savepoint()):
                response = self.submit()
                record.write(values)
                before = self.native_counts()
                self.process()
                self.assertEqual(self.job(response, self.operator_a).state, "failed")
                self.assertEqual(self.native_counts(), before)

    def test_native_business_error_after_real_second_line_rolls_back_quote_and_lines(self):
        response = self.submit()
        before = self.native_counts()
        line_class = type(self.env["sale.order.line"])
        real_create = line_class.create
        observed = []

        @api.model_create_multi
        def fail_after_real_lines(records, values):
            created = real_create(records, values)
            observed.extend(created.ids)
            if len(observed) >= 2:
                self.assertGreaterEqual(len(observed), 2)
                raise UserError("Sensitive credential must not enter audit: test-secret")
            return created

        with patch.object(line_class, "create", fail_after_real_lines):
            self.process()
        job = self.job(response, self.operator_a)
        self.assertGreaterEqual(len(observed), 2)
        self.assertEqual(job.state, "failed")
        self.assertEqual(self.native_counts(), before)
        event = self.attempts(job)
        self.assertEqual(event.event_type, "failed")
        self.assertNotIn("test-secret", (job.error_detail or "") + (event.error_detail or ""))

    def test_unknown_and_real_transient_errors_propagate_and_restore_pending_live(self):
        line_class = type(self.env["sale.order.line"])
        real_create = line_class.create
        for error_type in (ValueError, errors.SerializationFailure, errors.DeadlockDetected,
                           errors.LockNotAvailable):
            with self.subTest(error=error_type.__name__), closing(self.cr.savepoint()):
                response = self.submit()
                before = self.native_counts()

                @api.model_create_multi
                def fail_after_real_lines(records, values):
                    real_create(records, values)
                    raise error_type("injected fatal or transient")

                with patch.object(line_class, "create", fail_after_real_lines):
                    with unittest.TestCase.assertRaises(self, error_type):
                        self.worker()._process_pending()
                self.env.flush_all()
                self.env.invalidate_all()
                job = self.job(response, self.operator_a)
                self.assertEqual((job.state, job.attempt_count), ("pending", 0))
                self.assertFalse(job.last_attempt_at)
                self.assertFalse(job.sale_order_id)
                self.assertFalse(self.attempts(job))
                self.assertEqual(self.native_counts(), before)

    def test_worker_actual_role_company_and_source_guards_survive_sudo(self):
        response = self.submit()
        for user in (self.integration_a, self.sales_a, self.settings, self.plain, self.operator_b):
            model = self.worker(user).sudo().with_context(
                import_worker=True, default_company_id=self.company_a.id,
                default_state="done", force_company=self.company_a.id,
            )
            operations = (
                lambda: model._worker_authorized_source(self.source_a.sudo()),
                lambda: model.browse(response["job_id"])._worker_authorized_job(),
                lambda: model.browse(response["job_id"])._worker_lock_job(),
                lambda: model.browse(response["job_id"])._process_job(),
            )
            for operation in operations:
                with self.subTest(user=user.login), self.assertRaises(AccessError):
                    operation()
        foreign = self.submit(self.payload(self.source_b), user=self.integration_b)
        other = self.submit(self.payload(self.other_source), user=self.other_integration)
        for response in (foreign, other):
            with self.assertRaises(AccessError):
                self.worker().sudo().browse(response["job_id"])._process_job()
        self.assertEqual(self.job(response, self.other_operator).state, "pending")

    def test_elevated_or_multicompany_processor_is_rejected_without_mutation(self):
        stock_group = self.env.ref("stock.group_stock_user")
        for values in (
            {"groups_id": [Command.link(stock_group.id)]},
            {"groups_id": [Command.link(self.env.ref("sales_team.group_sale_salesman_all_leads").id)]},
            {"company_ids": [Command.set(self.companies.ids)]},
            {"active": False},
        ):
            try:
                with self.subTest(values=values), closing(self.cr.savepoint()):
                    self.operator_a.invalidate_recordset()
                    self.assertTrue(self.operator_a.active)
                    self.assertEqual(self.operator_a.company_ids, self.company_a)
                    self.assertNotIn(stock_group, self.operator_a.groups_id)
                    self.assertFalse(self.operator_a.has_group("stock.group_stock_user"))
                    self.assertFalse(self.operator_a.has_group("sales_team.group_sale_salesman_all_leads"))
                    response = self.submit()
                    self.operator_a.write(values)
                    with unittest.TestCase.assertRaises(self, AccessError):
                        self.worker().sudo()._process_pending()
                    self.env.flush_all()
                    self.env.invalidate_all()
                    self.assertEqual(self.job(response).state, "pending")
            finally:
                # Savepoint rollback clears record caches, but the native
                # res.users._get_group_ids ormcache belongs to the registry.
                self.env.registry.clear_cache()

    def test_worker_and_internal_services_are_not_rpc_methods(self):
        for name in ("_process_pending", "_process_job", "_worker_actor",
                     "_worker_authorized_source", "_worker_authorized_job", "_worker_lock_job"):
            with self.subTest(method=name), self.assertRaises(AccessError):
                get_public_method(self.worker(), name)
        self.assertTrue(self.worker()._fields["sale_order_id"].readonly)

    def test_creator_rules_allow_only_own_created_and_existing_own_sales_quotes(self):
        response = self.submit()
        self.process()
        quote = self.job(response, self.operator_a).sale_order_id
        self.assertEqual(quote.with_user(self.operator_a).read(["id"])[0]["id"], quote.id)
        unrelated = self.native["sale.order"].with_user(self.sales_a).with_context(
            allowed_company_ids=self.company_a.ids,
        ).create({"partner_id": self.customer.id, "user_id": self.sales_a.id,
                  "company_id": self.company_a.id, "pricelist_id": self.pricelist_a.id,
                  "warehouse_id": self.warehouse_a.id})
        with self.assertRaises(AccessError):
            unrelated.with_user(self.operator_a).read(["id"])
        self.assertFalse(self.operator_a.has_group("sales_team.group_sale_salesman_all_leads"))
        self.assertFalse(self.operator_a.has_group("sales_team.group_sale_manager"))
