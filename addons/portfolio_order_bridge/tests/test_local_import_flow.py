"""Real-role local imports stay isolated, immutable, and atomic on rerun/failure."""

import hashlib
import importlib
import json
import secrets
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from odoo import SUPERUSER_ID, Command
from odoo.addons.portfolio_trade.acceptance_data import ROLE_GROUPS, prepare_acceptance
from odoo.addons.portfolio_trade.local_finance import prepare_local_finance
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from ..models.access import ImportAccess

M1 = "portfolio_trade_acceptance"
FINANCE = "portfolio_trade_finance"
PROFILE = "portfolio_order_bridge_acceptance"
NAMESPACE = "portfolio_order_bridge_flow"
SOURCE = "portfolio.import.source"
JOB = "portfolio.import.job"
ATTEMPT = "portfolio.import.attempt"
PRODUCT_MAP = "portfolio.import.product.map"
RETRY = "portfolio.import.retry"
REASON = "Acceptance SKU mapping repaired"
IDENTITIES = {
    "job_success": JOB, "quote_success": "sale.order", "attempt_success": ATTEMPT,
    "job_repaired": JOB, "quote_repaired": "sale.order", "attempt_failed": ATTEMPT,
    "attempt_retry": ATTEMPT, "attempt_repaired": ATTEMPT, "product_map_repair": PRODUCT_MAP,
}
OBSERVED = (
    "res.users", "res.partner", "res.company", "product.product", "product.template",
    "product.supplierinfo", "product.pricelist", "res.groups", "res.currency", "ir.default",
    "ir.cron", "ir.actions.server", "ir.model.data", SOURCE, "portfolio.import.customer.map",
    PRODUCT_MAP, JOB, ATTEMPT, "sale.order", "sale.order.line", "portfolio.sale.approval",
    "purchase.order", "purchase.order.line", "account.move", "account.move.line",
    "stock.move", "stock.move.line", "stock.quant", "stock.picking", "mail.mail",
)


class _UndoMutation(Exception):
    pass


@tagged("post_install", "-at_install", "portfolio_local_import_flow")
class TestLocalImportFlow(TransactionCase):
    def setUp(self):
        super().setUp()
        helper = Path(__file__).resolve().parents[1] / "local_import_flow.py"
        self.assertTrue(helper.is_file(), "Plain local order import flow helper is missing")
        self.module = importlib.import_module("odoo.addons.portfolio_order_bridge.local_import_flow")
        self.assertTrue(callable(getattr(self.module, "run_local_import", None)))
        self.profile = importlib.import_module("odoo.addons.portfolio_order_bridge.local_acceptance")
        prepare_acceptance(
            self.env, {role: secrets.token_urlsafe(24) for role in ROLE_GROUPS}, allow_local=True,
        )
        self.company, self.other = self.owned("company_a", M1), self.owned("company_b", M1)
        self.native = self.env(context={"allowed_company_ids": self.company.ids, "lang": "en_US"})
        prepare_local_finance(self.native, allow_local=True)
        self.passwords = {role: secrets.token_urlsafe(32) for role in ("settings", "operator", "integration")}
        self.profile_report = self.profile.prepare_local_import(self.native, self.passwords, allow_local=True)

    def markers(self, namespace=NAMESPACE):
        return self.env["ir.model.data"].search([("module", "=", namespace)], order="name")

    def owned(self, key, namespace=PROFILE):
        marker = self.markers(namespace).filtered(lambda item: item.name == key)
        return self.env[marker.model].with_context(active_test=False).browse(marker.res_id).exists()

    def marker_facts(self, namespace=NAMESPACE):
        return tuple((item.name, item.model, item.res_id, item.noupdate) for item in self.markers(namespace))

    def actor(self, role, model=JOB):
        namespace = M1 if role == "sales" else PROFILE
        return self.native[model].with_user(self.owned(f"user_{role}", namespace)).with_context(
            allowed_company_ids=self.company.ids, lang="en_US",
        )

    def flow(self, **kwargs):
        return self.module.run_local_import(
            kwargs.pop("env", self.native), kwargs.pop("passwords", self.passwords),
            allow_local=kwargs.pop("allow_local", True), **kwargs,
        )

    def rows(self, model, domain=()):
        records = self.native[model].with_context(active_test=False)
        if isinstance(records, ImportAccess):
            return super(ImportAccess, records).search(list(domain), order="id")
        return records.search(list(domain), order="id")

    def table_digest(self, model, ids=None):
        table = self.env[model]._table
        where, parameters = (" WHERE t.id = ANY(%s)", [list(ids)]) if ids is not None else ("", [])
        self.cr.execute(f'SELECT to_jsonb(t)::text FROM "{table}" AS t{where} ORDER BY t.id', parameters)
        return hashlib.sha256(repr(self.cr.fetchall()).encode()).hexdigest()

    def snapshot(self):
        self.env.flush_all()
        self.env.invalidate_all()
        return tuple((model, self.table_digest(model)) for model in OBSERVED)

    def originals(self):
        self.env.flush_all()
        self.env.invalidate_all()
        grouped = {}
        for namespace in (M1, FINANCE):
            for marker in self.markers(namespace):
                grouped.setdefault(marker.model, set()).add(marker.res_id)
        digests = tuple((model, self.table_digest(model, ids)) for model, ids in sorted(grouped.items()))
        globals_ = tuple((model, self.table_digest(model)) for model in
                         ("res.groups", "res.currency", "ir.default", "product.pricelist"))
        return self.marker_facts(M1), self.marker_facts(FINANCE), digests, globals_

    def assert_atomic_denied(self, operation, exception=UserError):
        before = self.snapshot()
        # stdlib assertion does not roll back an Odoo savepoint for the helper.
        with unittest.TestCase.assertRaises(self, exception):
            operation()
        self.assertEqual(self.snapshot(), before)

    @contextmanager
    def mutation(self):
        try:
            with self.cr.savepoint():
                yield
                raise _UndoMutation()
        except _UndoMutation:
            self.env.registry.clear_cache()
            self.env.invalidate_all()

    def assert_actor(self, records, role):
        expected = self.owned(f"user_{role}", M1 if role == "sales" else PROFILE)
        self.assertEqual(records.env.uid, expected.id)
        self.assertNotEqual(records.env.uid, SUPERUSER_ID)
        self.assertFalse(records.env.su)
        self.assertEqual(records.env.companies, self.company)
        self.assertEqual(records.env.context.get("lang"), "en_US")
        self.assertFalse(any(key.startswith("default_") for key in records.env.context))

    def test_real_role_flow_has_nine_exact_identities_two_drafts_four_events_and_no_erp_effects(self):
        original, static = self.originals(), self.marker_facts(PROFILE)
        unchanged = {model: self.table_digest(model) for model in
                     ("stock.move", "stock.move.line", "stock.quant", "stock.picking",
                      "purchase.order", "purchase.order.line", "account.move", "account.move.line",
                      "portfolio.sale.approval")}
        result = self.flow()
        self.assertEqual(self.module.EXPECTED_IDENTITIES, IDENTITIES)
        self.assertEqual({item.name: item.model for item in self.markers()}, IDENTITIES)
        self.assertTrue(all(self.markers().mapped("noupdate")))
        self.assertEqual(len({(item.model, item.res_id) for item in self.markers()}), 9)
        self.assertEqual(self.originals(), original)
        self.assertEqual(self.marker_facts(PROFILE), static)
        for model, before in unchanged.items():
            self.assertEqual(self.table_digest(model), before, model)
        self.assertEqual((result["schema_version"], result["database"], result["namespace"]),
                         (1, self.cr.dbname, NAMESPACE))
        self.assertEqual((result["company_id"], result["source_id"]),
                         (self.company.id, self.owned("source").id))
        self.assertEqual(result["identities"], {
            key: {"model": model, "id": self.owned(key, NAMESPACE).id}
            for key, model in IDENTITIES.items()
        })
        for suffix, attempts, external in (("success", 1, "BRIDGE-ACCEPTANCE-SUCCESS"),
                                            ("repaired", 2, "BRIDGE-ACCEPTANCE-RETRY")):
            job = self.actor("operator").browse(self.owned(f"job_{suffix}", NAMESPACE).id)
            quote = self.actor("sales", "sale.order").browse(self.owned(f"quote_{suffix}", NAMESPACE).id)
            self.assertEqual((job.state, job.attempt_count, job.external_order_id), ("done", attempts, external))
            self.assertEqual(job.sale_order_id, quote)
            self.assertEqual(job.submitted_by_id, self.owned("user_integration"))
            self.assertEqual(job.source_id.id, self.owned("source").id)
            self.assertEqual(job.company_id, self.company)
            self.assertEqual(quote.create_uid, self.owned("user_operator"))
            self.assertEqual(quote.portfolio_creator_id, self.owned("user_operator"))
            self.assertEqual(quote.user_id, self.owned("user_sales", M1))
            self.assertEqual(quote.state, "draft")
            self.assertEqual(quote.currency_id.name, "CNY")
            self.assertEqual(quote.partner_id, self.owned("customer"))
            self.assertEqual(quote.warehouse_id, self.owned("warehouse_a", M1))
            self.assertEqual(quote.pricelist_id, self.owned("pricelist_cny", FINANCE))
            self.assertTrue(quote._portfolio_requires_approval())
            self.assertFalse(quote.portfolio_approval_history_ids)
            self.assertFalse(quote.order_line.tax_id)
            self.assertIn(15.0, quote.order_line.mapped("discount"))
            self.assertTrue(all(product in (self.owned("product_item1") | self.owned("product_item2"))
                                for product in quote.order_line.product_id))
        for key, kind, ordinal, suffix in (
            ("attempt_success", "done", 1, "success"), ("attempt_failed", "failed", 1, "repaired"),
            ("attempt_retry", "retry", 1, "repaired"), ("attempt_repaired", "done", 2, "repaired"),
        ):
            event = self.actor("operator", ATTEMPT).browse(self.owned(key, NAMESPACE).id)
            self.assertEqual((event.event_type, event.attempt_no), (kind, ordinal))
            self.assertEqual(event.actor_id, self.owned("user_operator"))
            self.assertEqual(event.job_id.id, self.owned(f"job_{suffix}", NAMESPACE).id)
            self.assertEqual(event.company_id, self.company)
            self.assertTrue(event.started_at and event.finished_at >= event.started_at)
        failure = self.actor("operator", ATTEMPT).browse(self.owned("attempt_failed", NAMESPACE).id)
        raw = json.loads(self.actor("operator").browse(self.owned("job_repaired", NAMESPACE).id).raw_payload)
        missing_index = next(index for index, line in enumerate(raw["lines"])
                             if line["sku"] == "BRIDGE-ACCEPTANCE-REPAIR")
        self.assertEqual((failure.error_code, failure.error_path),
                         ("product_mapping", f"lines[{missing_index}].sku"))
        retry = self.actor("operator", ATTEMPT).browse(self.owned("attempt_retry", NAMESPACE).id)
        self.assertEqual(retry.retry_reason, REASON)
        self.assertEqual(retry.mapping_snapshot, failure.mapping_snapshot)
        repair = self.actor("settings", PRODUCT_MAP).browse(self.owned("product_map_repair", NAMESPACE).id)
        self.assertEqual((repair.external_sku, repair.product_id, repair.create_uid),
                         ("BRIDGE-ACCEPTANCE-REPAIR", self.owned("product_item2"), self.owned("user_settings")))
        for index in (1, 2):
            self.assertEqual(self.owned(f"product_item{index}").with_company(self.company).qty_available, 0)
        self.assertEqual(result["m1_preservation"]["bearing_stock"],
                         self.owned("product_bearing", M1).with_company(self.company).qty_available)

    def test_public_services_use_actual_ordinary_roles_and_duplicate_ingress_reuses_same_job(self):
        seen = {key: [] for key in ("submit", "process", "mapping", "wizard", "retry")}
        job_type, map_type = type(self.native[JOB]), type(self.native[PRODUCT_MAP])
        wizard_type = type(self.native[RETRY])
        originals = {"submit": job_type.submit_order, "process": job_type.action_process_pending,
                     "mapping": map_type.create, "wizard": wizard_type.create,
                     "retry": wizard_type.action_apply}

        def wrapper(key, role):
            def call(records, *args, **kwargs):
                self.assert_actor(records, role)
                result = originals[key](records, *args, **kwargs)
                seen[key].append((args, result))
                return result
            return call

        with patch.object(job_type, "submit_order", wrapper("submit", "integration")), \
                patch.object(job_type, "action_process_pending", wrapper("process", "operator")), \
                patch.object(map_type, "create", wrapper("mapping", "settings")), \
                patch.object(wizard_type, "create", wrapper("wizard", "operator")), \
                patch.object(wizard_type, "action_apply", wrapper("retry", "operator")):
            self.flow()
        success = [result for args, result in seen["submit"]
                   if args[0]["external_order_id"] == "BRIDGE-ACCEPTANCE-SUCCESS"]
        self.assertGreaterEqual(len(success), 2)
        self.assertEqual(success[0]["job_id"], success[1]["job_id"])
        self.assertFalse(success[0]["reused"])
        self.assertTrue(success[1]["reused"])
        self.assertEqual(len(seen["process"]), 3)
        self.assertEqual(len(seen["mapping"]), 1)
        self.assertEqual(len(seen["wizard"]), 1)
        self.assertEqual(len(seen["retry"]), 1)
        self.assertEqual(seen["wizard"][0][0][0]["reason"], REASON)

    def test_sales_confirmation_rejects_exact_business_approval_without_state_or_audit_change(self):
        seen = []
        sale_type = type(self.native["sale.order"])
        original = sale_type.action_confirm

        def confirm(records):
            self.assert_actor(records, "sales")
            before = self.table_digest("portfolio.sale.approval")
            try:
                result = original(records)
            except UserError as error:
                self.assertIs(type(error), UserError)
                self.assertEqual(str(error), "This quotation requires current approval before confirmation. Submit it for approval.")
                self.assertEqual(records.state, "draft")
                self.assertEqual(self.table_digest("portfolio.sale.approval"), before)
                seen.append(records.id)
                raise
            self.fail(f"Acceptance quotation unexpectedly confirmed: {result!r}")

        with patch.object(sale_type, "action_confirm", confirm):
            self.flow()
        self.assertTrue(seen)

    def test_failed_event_is_written_before_repair_and_is_immutable_after_native_retry(self):
        map_type = type(self.native[PRODUCT_MAP])
        original, failed = map_type.create, []

        def create(records, values):
            self.assert_actor(records, "settings")
            job = self.actor("operator").search([("external_order_id", "=", "BRIDGE-ACCEPTANCE-RETRY")])
            self.assertEqual((job.state, job.attempt_count), ("failed", 1))
            self.assertFalse(job.sale_order_id)
            event = self.actor("operator", ATTEMPT).search([("job_id", "=", job.id)])
            self.assertEqual((event.event_type, event.error_code), ("failed", "product_mapping"))
            failed.append((event.id, self.table_digest(ATTEMPT, event.ids)))
            return original(records, values)

        with patch.object(map_type, "create", create):
            self.flow()
        self.assertEqual(len(failed), 1)
        event_id, digest = failed[0]
        self.assertEqual(self.table_digest(ATTEMPT, [event_id]), digest)

    def test_completed_rerun_is_read_only_and_json_bytes_are_identical(self):
        first = self.flow()
        before = self.snapshot()
        forbidden = {
            JOB: ("submit_order", "action_process_pending", "_process_pending", "action_retry"),
            SOURCE: ("create", "write", "action_prepare_processing_cron"),
            PRODUCT_MAP: ("create", "write"), RETRY: ("create", "write", "action_apply"),
            "sale.order": ("create", "write", "action_confirm"),
            "sale.order.line": ("create", "write"),
            "stock.quant": ("create", "write"), "res.users": ("write", "_set_encrypted_password"),
        }
        with ExitStack() as stack:
            for model, methods in forbidden.items():
                for method in methods:
                    stack.enter_context(patch.object(type(self.native[model]), method,
                                                     side_effect=AssertionError(f"Rerun mutated {model}.{method}")))
            second = self.flow()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(self.snapshot(), before)
        for forbidden in ("password", "credential", "cookie", "dsn", "raw_payload", "db_host", "payload_hash"):
            self.assertNotIn(forbidden, json.dumps(second).lower())
        for value in self.passwords.values():
            self.assertNotIn(value, json.dumps(second))

    def test_static_profile_after_complete_flow_remains_read_only_and_has_same_seventeen_ids(self):
        self.flow()
        before = self.snapshot()
        result = self.profile.prepare_local_import(self.native, self.passwords, allow_local=True)
        self.assertEqual(result, self.profile_report)
        self.assertEqual(len(result["identities"]), 17)
        self.assertEqual(self.snapshot(), before)

    def test_unmarked_repair_alias_and_partial_flow_markers_do_not_relax_static_profile(self):
        repair = self.actor("settings", PRODUCT_MAP).create({
            "source_id": self.owned("source").id, "external_sku": "BRIDGE-ACCEPTANCE-REPAIR",
            "product_id": self.owned("product_item2").id,
        })
        self.assert_atomic_denied(lambda: self.profile.prepare_local_import(self.native, self.passwords, allow_local=True))
        self.env["ir.model.data"].create({
            "module": NAMESPACE, "name": "product_map_repair", "model": PRODUCT_MAP,
            "res_id": repair.id, "noupdate": True,
        })
        self.assert_atomic_denied(lambda: self.profile.prepare_local_import(self.native, self.passwords, allow_local=True))
        self.assert_atomic_denied(lambda: self.flow())

    def test_missing_static_profile_refuses_without_creating_users_catalog_or_business_records(self):
        self.markers(PROFILE).unlink()
        with patch.object(self.profile, "_create_users", side_effect=AssertionError("Missing profile cannot bootstrap")):
            self.assert_atomic_denied(lambda: self.flow())

    def test_opt_in_password_actor_and_actual_company_scope_fail_before_writes(self):
        denied = (
            ({"allow_local": False}, UserError), ({"allow_local": 1}, UserError),
            ({"passwords": {**self.passwords, "operator": "incorrect-private-password-value"}}, UserError),
            ({"env": self.actor("sales").sudo().env}, AccessError),
            ({"env": self.native(context={"allowed_company_ids": self.other.ids})}, AccessError),
            ({"env": self.native(context={"allowed_company_ids": [self.company.id, 2147483647]})}, AccessError),
        )
        for kwargs, exception in denied:
            with self.subTest(kwargs=list(kwargs)):
                self.assert_atomic_denied(lambda: self.flow(**kwargs), exception)

    def test_actual_postgres_host_and_database_are_checked_before_writes(self):
        for database, host in (("production", "127.0.0.1"), (self.cr.dbname, "remote.invalid"),
                               (self.cr.dbname, "localhost"), (self.cr.dbname, "")):
            with self.subTest(database=database, host=host):
                before = self.snapshot()
                cursor = SimpleNamespace(dbname=database, _cnx=SimpleNamespace(info=SimpleNamespace(host=host)))
                fake = SimpleNamespace(cr=cursor)
                with unittest.TestCase.assertRaises(self, UserError):
                    self.module.run_local_import(fake, self.passwords, allow_local=True)
                self.assertEqual(self.snapshot(), before)

    def test_modified_profile_source_cron_role_or_company_rejects_before_business_mutation(self):
        edits = (
            lambda: self.actor("settings", SOURCE).browse(self.owned("source").id).write({"active": False}),
            lambda: self.owned("processing_cron").write({"active": True}),
            lambda: self.owned("user_operator").write({"groups_id": [Command.link(self.env.ref("stock.group_stock_user").id)]}),
            lambda: self.owned("user_integration").write({"company_ids": [Command.link(self.other.id)]}),
            lambda: self.company.write({"portfolio_approval_enabled": False}),
        )
        for edit in edits:
            with self.subTest(edit=edit), self.mutation():
                edit()
                self.env.registry.clear_cache()
                self.assert_atomic_denied(lambda: self.flow())

    def test_forged_caller_defaults_and_stock_filters_are_discarded_before_roles_and_facts(self):
        original = self.originals()
        context = {"allowed_company_ids": self.company.ids, "default_company_id": self.other.id,
                   "default_user_id": SUPERUSER_ID, "default_state": "sale", "default_discount": 99,
                   "location": -1, "warehouse": -1, "to_date": "1900-01-01", "lang": "zh_CN",
                   "force_company": self.other.id}
        self.flow(env=self.native(context=context))
        self.assertEqual(self.originals(), original)
        for suffix in ("success", "repaired"):
            quote = self.owned(f"quote_{suffix}", NAMESPACE)
            self.assertEqual((quote.company_id, quote.state), (self.company, "draft"))

    def test_existing_flow_missing_wrong_model_stale_repoint_extra_and_noupdate_conflicts(self):
        self.flow()
        alterations = (
            lambda: self.markers().filtered(lambda item: item.name == "attempt_retry").unlink(),
            lambda: self.markers().filtered(lambda item: item.name == "job_success").write({"model": "sale.order"}),
            lambda: self.markers().filtered(lambda item: item.name == "quote_success").write({"res_id": 2147483647}),
            lambda: self.markers().filtered(lambda item: item.name == "job_success").write({"res_id": self.owned("job_repaired", NAMESPACE).id}),
            lambda: self.markers().filtered(lambda item: item.name == "quote_success").write({"res_id": self.owned("quote_repaired", NAMESPACE).id}),
            lambda: self.markers().filtered(lambda item: item.name == "attempt_retry").write({"noupdate": False}),
            lambda: self.env["ir.model.data"].create({"module": NAMESPACE, "name": "extra", "model": "res.company", "res_id": self.company.id}),
        )
        for alter in alterations:
            with self.subTest(alter=alter), self.mutation():
                alter()
                self.assert_atomic_denied(lambda: self.flow())

    def test_changed_raw_payload_hash_attempt_and_quote_are_never_rebuilt(self):
        self.flow()
        alterations = (
            ("job_success", {"raw_payload": "{}"}),
            ("job_repaired", {"payload_hash": "0" * 64}),
            ("attempt_failed", {"error_path": "lines[99].sku"}),
            ("attempt_retry", {"retry_reason": "User changed audit reason"}),
            ("attempt_repaired", {"actor_id": self.owned("user_integration").id}),
        )
        for key, values in alterations:
            with self.subTest(key=key), self.mutation():
                record = self.owned(key, NAMESPACE)
                super(ImportAccess, record).write(values)
                self.assert_atomic_denied(lambda: self.flow())
        with self.mutation():
            self.actor("sales", "sale.order").browse(self.owned("quote_success", NAMESPACE).id).order_line[:1].write({"price_unit": 999})
            self.assert_atomic_denied(lambda: self.flow())

    def test_completed_repair_map_wrong_target_inactive_alias_and_unmarked_extra_conflict(self):
        self.flow()
        mapping = self.actor("settings", PRODUCT_MAP).browse(self.owned("product_map_repair", NAMESPACE).id)
        for values in ({"product_id": self.owned("product_item1").id}, {"active": False},
                       {"external_sku": "USER-CHANGED-REPAIR"}):
            with self.subTest(values=values), self.mutation():
                mapping.write(values)
                self.assert_atomic_denied(lambda: self.flow())
                self.assert_atomic_denied(lambda: self.profile.prepare_local_import(self.native, self.passwords, allow_local=True))
        with self.mutation():
            self.actor("settings", PRODUCT_MAP).create({
                "source_id": self.owned("source").id, "external_sku": "UNKNOWN-EXTRA-MAP",
                "product_id": self.owned("product_item1").id,
            })
            self.assert_atomic_denied(lambda: self.flow())
            self.assert_atomic_denied(lambda: self.profile.prepare_local_import(self.native, self.passwords, allow_local=True))

    def test_completed_marker_foreign_company_and_repair_creator_are_rejected_without_repair(self):
        self.flow()
        alterations = (
            ("job_success", "portfolio_import_job", "company_id", self.other.id),
            ("product_map_repair", "portfolio_import_product_map", "create_uid", SUPERUSER_ID),
        )
        for key, table, column, value in alterations:
            with self.subTest(key=key), self.mutation():
                # Legacy corruption fixture only: native write intentionally
                # drops create_uid, while changing job company traverses guarded
                # attempt relations before an inactive technical root can write.
                # No production service/state transition uses this SQL path.
                self.env.flush_all()
                record_id = self.owned(key, NAMESPACE).id
                self.cr.execute(f'UPDATE "{table}" SET "{column}" = %s WHERE id = %s',
                                [value, record_id])
                self.env.invalidate_all()
                self.cr.execute(f'SELECT "{column}" FROM "{table}" WHERE id = %s', [record_id])
                self.assertEqual(self.cr.fetchone()[0], value)
                self.assert_atomic_denied(lambda: self.flow())
                self.assert_atomic_denied(lambda: self.profile.prepare_local_import(self.native, self.passwords, allow_local=True))

    def test_unknown_stock_quant_for_owned_product_is_rejected_without_inventory_reset(self):
        self.native["stock.quant"].create({
            "product_id": self.owned("product_item1").id,
            "location_id": self.owned("warehouse_a", M1).lot_stock_id.id,
            "quantity": 1,
        })
        self.assert_atomic_denied(lambda: self.flow())

    def test_preexisting_unmarked_matching_job_is_not_adopted_or_processed(self):
        payload = {"company_code": "PARTS-A", "source_code": "B2B-ACCEPTANCE",
                   "external_order_id": "BRIDGE-ACCEPTANCE-SUCCESS",
                   "customer_ref": "BRIDGE-ACCEPTANCE-CUSTOMER", "currency": "CNY",
                   "lines": [{"external_line_id": "1", "sku": "BRIDGE-ACCEPTANCE-1",
                              "quantity": "2", "unit_price": "120", "discount": "15"}]}
        self.actor("integration").submit_order(payload)
        self.assert_atomic_denied(lambda: self.flow())
        self.assertFalse(self.markers())

    def test_unexpected_source_queue_is_never_processed_by_fixed_size_batch(self):
        payload = {"company_code": "PARTS-A", "source_code": "B2B-ACCEPTANCE",
                   "external_order_id": "USER-UNEXPECTED-QUEUE", "customer_ref": "BRIDGE-ACCEPTANCE-CUSTOMER",
                   "currency": "CNY", "lines": [{"external_line_id": "1", "sku": "BRIDGE-ACCEPTANCE-1",
                                                 "quantity": "1", "unit_price": "120", "discount": "0"}]}
        response = self.actor("integration").submit_order(payload)
        self.assert_atomic_denied(lambda: self.flow())
        self.assertEqual(self.actor("operator").browse(response["job_id"]).state, "pending")

    def test_other_source_assigned_to_dedicated_operator_rejects_before_processing_either_queue(self):
        source = self.actor("settings", SOURCE).create({
            "name": "User source sharing acceptance operator", "company_id": self.company.id,
            "company_code": "PARTS-A", "code": "USER-OTHER-SOURCE",
            "warehouse_id": self.owned("warehouse_a", M1).id,
            "pricelist_id": self.owned("pricelist_cny", FINANCE).id,
            "sales_user_id": self.owned("user_sales", M1).id,
            "processor_user_id": self.owned("user_operator").id,
            "integration_user_ids": [Command.set(self.owned("user_integration").ids)],
        })
        payload = {"company_code": "PARTS-A", "source_code": source.code,
                   "external_order_id": "USER-OTHER-SOURCE-ORDER", "customer_ref": "UNKNOWN-CUSTOMER",
                   "currency": "CNY", "lines": [{"external_line_id": "1", "sku": "UNKNOWN-SKU",
                                                 "quantity": "1", "unit_price": "120", "discount": "0"}]}
        response = self.actor("integration").submit_order(payload)
        processor_type = type(self.native[JOB])
        with patch.object(processor_type, "action_process_pending",
                          side_effect=AssertionError("Unknown source queue must not be processed")):
            self.assert_atomic_denied(lambda: self.flow())
        self.assertEqual(self.actor("operator").browse(response["job_id"]).state, "pending")
        self.assertFalse(self.markers())

    def test_unmarked_product_sale_history_is_not_adopted(self):
        self.actor("sales", "sale.order").create({
            "partner_id": self.owned("customer").id, "company_id": self.company.id,
            "user_id": self.owned("user_sales", M1).id, "warehouse_id": self.owned("warehouse_a", M1).id,
            "pricelist_id": self.owned("pricelist_cny", FINANCE).id,
            "order_line": [Command.create({"product_id": self.owned("product_item1").id,
                                            "product_uom_qty": 1, "price_unit": 120,
                                            "tax_id": [Command.clear()]})],
        })
        self.assert_atomic_denied(lambda: self.flow())

    def test_complete_flow_then_unknown_job_or_product_history_is_rejected_read_only(self):
        self.flow()
        with self.mutation():
            self.actor("sales", "sale.order").browse(self.owned("quote_success", NAMESPACE).id).order_line.create({
                "order_id": self.owned("quote_success", NAMESPACE).id,
                "product_id": self.owned("product_item1").id, "product_uom_qty": 1,
                "price_unit": 1, "tax_id": [Command.clear()],
            })
            self.assert_atomic_denied(lambda: self.flow())

    def test_exception_after_first_real_quote_rolls_back_all_dynamic_rows_inside_helper(self):
        quote_type = type(self.native["sale.order"])
        original, seen = quote_type.create, []

        def create(records, values):
            quote = original(records, values)
            seen.extend(quote.ids)
            raise RuntimeError("Injected failure after real quotation")

        with patch.object(quote_type, "create", create):
            self.assert_atomic_denied(lambda: self.flow(), RuntimeError)
        self.assertTrue(seen)
        self.assertFalse(self.markers())
        self.assertEqual(len(self.markers(PROFILE)), 17)

    def test_exception_after_first_real_attempt_rolls_back_all_dynamic_rows_inside_helper(self):
        attempt_type = type(self.native[ATTEMPT])
        original, seen = attempt_type._append_event, []

        def append(records, *args, **kwargs):
            event = original(records, *args, **kwargs)
            seen.extend(event.ids)
            raise RuntimeError("Injected failure after real attempt")

        with patch.object(attempt_type, "_append_event", append):
            self.assert_atomic_denied(lambda: self.flow(), RuntimeError)
        self.assertTrue(seen)
        self.assertFalse(self.markers())
        self.assertEqual(len(self.markers(PROFILE)), 17)
