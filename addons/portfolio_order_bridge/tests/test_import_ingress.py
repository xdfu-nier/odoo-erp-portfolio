"""Native reception, immutable audit, authorization and rollback contracts."""

import copy
import unittest
from contextlib import closing

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.service.model import get_public_method
from odoo.tests import tagged
from odoo.tools import mute_logger
from psycopg2 import IntegrityError

from ..payload import normalize_order_payload
from .common import ImportIngressCase


@tagged("post_install", "-at_install", "portfolio_import_ingress")
class TestImportIngress(ImportIngressCase):
    AUDIT = (
        "source_id", "company_id", "external_order_id", "payload_hash", "raw_payload",
        "canonical_payload", "submitted_by_id", "received_at", "state", "attempt_count",
        "last_attempt_at", "error_code", "error_detail", "sale_order_id",
    )

    def setUp(self):
        super().setUp()
        self.assertTrue(self.JOB in self.env.registry, "M3 ingress job model missing")
        self.sequence = 0
        self.source_a = self.source()
        self.source_b = self.source(self.company_b)
        self.other_source = self.source(
            processor_user_id=self.other_operator.id,
            integration_user_ids=[Command.set(self.other_integration.ids)],
        )

    def assert_atomic_rejection(self, payload, exceptions=(AccessError, UserError, ValidationError),
                                actor=None):
        model = actor if actor is not None else self.actor()
        before = self.actor().search([]).read(list(self.AUDIT))
        revision = self.source_a.ingress_revision
        # The inner assert intentionally does not use Odoo's rollback-aware helper.
        # A caught business error must leave the live transaction/cache unchanged.
        with self.cr.savepoint():
            with unittest.TestCase.assertRaises(self, exceptions) as caught:
                model.submit_order(payload)
            self.env.flush_all()
            self.env.invalidate_all()
            self.assertEqual(self.actor().search([]).read(list(self.AUDIT)), before)
            self.assertEqual(self.source_a.ingress_revision, revision)
        return str(caught.exception)

    def test_reception_records_exact_audit_and_pending_response(self):
        payload = self.payload()
        normalized = normalize_order_payload(payload)
        before = fields.Datetime.now()
        response = self.submit(payload)
        self.assertEqual(set(response), {"job_id", "state", "sale_order_id", "reused"})
        self.assertEqual(response, {
            "job_id": response["job_id"], "state": "pending", "sale_order_id": False,
            "reused": False,
        })
        self.assertIs(type(response["job_id"]), int)
        job = self.job(response)
        self.assertEqual(job.source_id, self.source_a)
        self.assertEqual(job.company_id, self.company_a)
        self.assertEqual(job.external_order_id, payload["external_order_id"])
        self.assertEqual(job.raw_payload, normalized.raw_json)
        self.assertEqual(job.canonical_payload, normalized.canonical_json)
        self.assertEqual(job.payload_hash, normalized.payload_hash)
        self.assertEqual(job.submitted_by_id, self.integration_a)
        self.assertGreaterEqual(job.received_at, before)
        self.assertLessEqual(job.received_at, fields.Datetime.now())
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.attempt_count, 0)
        self.assertFalse(job.last_attempt_at)
        self.assertFalse(job.error_code)
        self.assertFalse(job.error_detail)
        self.assertFalse(job.sale_order_id)
        self.assertEqual(self.source_a.ingress_revision, 1)

    def test_reception_has_no_sales_stock_account_or_approval_side_effects(self):
        names = ("sale.order", "sale.order.line", "portfolio.sale.approval", "stock.picking",
                 "stock.move", "account.move", "account.move.line")
        before = {name: self.native[name].search_count([]) for name in names}
        self.submit()
        self.assertEqual({name: self.native[name].search_count([]) for name in names}, before)

    def test_missing_maps_are_pending_without_creating_master_data(self):
        names = ("res.partner", "product.product", "portfolio.import.customer.map",
                 "portfolio.import.product.map")
        observers = {
            name: self.actor(name, self.settings, self.companies)
            if name.startswith("portfolio.import.") else self.native[name]
            for name in names
        }
        before = {name: model.search_count([]) for name, model in observers.items()}
        job = self.job(self.submit())
        self.assertEqual(job.state, "pending")
        self.assertEqual({name: model.search_count([]) for name, model in observers.items()}, before)

    def test_same_payload_reuses_one_job_and_retains_first_audit(self):
        original = self.submit()
        before = self.job(original).read(list(self.AUDIT))
        reused = self.submit()
        self.assertEqual(reused, {**original, "reused": True})
        self.assertEqual(self.actor().search_count([]), 1)
        self.assertEqual(self.job(original).read(list(self.AUDIT)), before)

    def test_equivalent_decimal_line_order_and_transport_reuse_original_raw(self):
        original = self.submit()
        before = self.job(original).read(list(self.AUDIT))
        payload = self.payload(request_id="different-request", sent_at="later")
        payload["lines"].reverse()
        payload["lines"][0].update(quantity="0002.000000", unit_price="020.000", discount="0.000")
        payload["lines"][1].update(quantity="01.500", unit_price="012.500000", discount="01")
        self.assertEqual(self.submit(payload), {**original, "reused": True})
        self.assertEqual(self.job(original).read(list(self.AUDIT)), before)
        self.assertEqual(self.actor().search_count([]), 1)

    def test_changed_business_meaning_conflicts_without_audit_or_revision_changes(self):
        self.submit()
        for key, value in (("quantity", "3"), ("unit_price", "12.51"), ("discount", "2")):
            payload = self.payload()
            payload["lines"][0][key] = value
            with self.subTest(key=key):
                message = self.assert_atomic_rejection(payload, (UserError,))
                self.assertIn("conflict", message.lower())
        self.assert_atomic_rejection(self.payload(customer_ref="DIFFERENT-CUSTOMER"), (UserError,))

    def test_external_id_source_and_company_form_independent_keys(self):
        first = self.submit(user=self.integration_both)
        second = self.submit(self.payload(external_order_id="ORDER-2"), user=self.integration_both)
        third = self.submit(self.payload(self.source_b), user=self.integration_both)
        source = self.source()
        fourth = self.submit(self.payload(source), user=self.integration_both)
        self.assertEqual(len({item["job_id"] for item in (first, second, third, fourth)}), 4)
        self.assertTrue(all(item["state"] == "pending" for item in (first, second, third, fourth)))

    def test_invalid_payload_has_safe_code_and_path_without_sensitive_echo(self):
        payload = self.payload(customer_ref="SECRET-CUSTOMER")
        payload["lines"][0]["unit_price"] = "SECRET-PRICE"
        message = self.assert_atomic_rejection(payload, (UserError,))
        self.assertIn("invalid_decimal", message)
        self.assertIn("lines[0].unit_price", message)
        self.assertNotIn("SECRET-CUSTOMER", message)
        self.assertNotIn("SECRET-PRICE", message)

    def test_schema_commands_dynamic_models_and_anonymous_payloads_are_rejected(self):
        invalid = (None, [], "payload", {**self.payload(), "model": "sale.order"},
                   {**self.payload(), "lines": [(0, 0, {"name": "SECRET-COMMAND"})]})
        for payload in invalid:
            with self.subTest(payload_type=type(payload).__name__):
                self.assert_atomic_rejection(payload, (UserError,))

    def test_actual_internal_integration_role_is_required_before_payload_parsing(self):
        portal = self.make_user("portal", "base.group_portal")
        public = self.env.ref("base.public_user")
        for user in (self.plain, self.settings, self.sales_a, self.operator_a, portal, public):
            with self.subTest(user=user.login):
                model = self.actor(user=user).sudo().with_context(uid=self.integration_a.id,
                                                                  import_internal=True)
                with self.assertRaises(AccessError):
                    model.submit_order(None)

    def test_inactive_actual_integration_user_is_rejected(self):
        self.integration_a.write({"active": False})
        with self.assertRaises(AccessError):
            self.actor().sudo().submit_order(self.payload())

    def test_unknown_or_unauthorized_source_is_rejected_without_job(self):
        for payload in (self.payload(source_code="UNKNOWN"),
                        self.payload(company_code="UNKNOWN"), self.payload(self.other_source)):
            with self.subTest(source=payload["source_code"]):
                self.assert_atomic_rejection(payload)

    def test_inactive_source_is_rejected_even_with_active_test_false(self):
        self.source_a.write({"active": False})
        self.assert_atomic_rejection(self.payload(), actor=self.actor().with_context(active_test=False))

    def test_company_scope_requires_actual_membership_and_explicit_active_scope(self):
        with self.assertRaises(AccessError):
            self.actor().sudo().with_context(allowed_company_ids=self.companies.ids).submit_order(
                self.payload(self.source_b),
            )
        with unittest.TestCase.assertRaises(self, (AccessError, UserError)):
            self.actor(user=self.integration_both, companies=self.company_a).submit_order(
                self.payload(self.source_b),
            )
        self.assertFalse(self.actor().search([]))

    def test_context_defaults_and_internal_flags_cannot_forge_server_fields(self):
        model = self.actor().sudo().with_context(
            uid=self.settings.id, force_company=self.company_b.id,
            default_company_id=self.company_b.id, default_source_id=self.source_b.id,
            default_state="done", default_sale_order_id=999999,
            default_submitted_by_id=self.settings.id, default_attempt_count=99,
            import_internal=True, _import_internal=True,
        )
        job = self.job(model.submit_order(self.payload()))
        self.assertEqual(job.company_id, self.company_a)
        self.assertEqual(job.source_id, self.source_a)
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.attempt_count, 0)
        self.assertEqual(job.submitted_by_id, self.integration_a)
        self.assertFalse(job.sale_order_id)

    def test_ir_defaults_cannot_forge_job_state_or_technical_source_revision(self):
        defaults = self.env["ir.default"]
        defaults.set(self.JOB, "state", "done")
        defaults.set(self.JOB, "attempt_count", 88)
        defaults.set(self.SOURCE, "ingress_revision", 700)
        source = self.source()
        self.assertEqual(source.ingress_revision, 0)
        job = self.job(self.submit())
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.attempt_count, 0)

    def test_source_revision_is_server_owned_even_for_settings_and_sudo(self):
        self.assertTrue(self.source_a._fields["ingress_revision"].readonly)
        for record in (self.source_a, self.source_a.sudo()):
            with unittest.TestCase.assertRaises(self, (AccessError, UserError)):
                record.write({"ingress_revision": 9})
        for model in (self.actor(self.SOURCE, self.settings), self.actor(self.SOURCE, self.settings).sudo()):
            with unittest.TestCase.assertRaises(self, (AccessError, UserError)):
                model.create(self.source_values(ingress_revision=9))
        source = self.actor(self.SOURCE, self.settings).with_context(default_ingress_revision=900).create(
            self.source_values(),
        )
        self.assertEqual(source.ingress_revision, 0)

    def test_source_current_currency_and_pricelist_are_revalidated(self):
        for record, values in ((self.company_a, {"currency_id": self.usd.id}),
                               (self.pricelist_a, {"currency_id": self.usd.id}),
                               (self.pricelist_a, {"active": False}),
                               (self.warehouse_a, {"active": False})):
            with self.subTest(model=record._name, values=values), closing(self.cr.savepoint()):
                record.write(values)
                self.assert_atomic_rejection(self.payload())

    def test_source_current_processor_privileges_and_archived_users_are_revalidated(self):
        for user, values in (
            (self.operator_a, {"groups_id": [Command.link(self.env.ref("stock.group_stock_user").id)]}),
            (self.operator_a, {"active": False}), (self.sales_a, {"active": False}),
            (self.integration_both, {"active": False}),
        ):
            with self.subTest(user=user.login, values=values), closing(self.cr.savepoint()):
                user.write(values)
                self.assert_atomic_rejection(self.payload())

    def test_removed_integration_authorization_prevents_resubmission(self):
        self.submit()
        stale = self.actor()
        self.source_a.write({"integration_user_ids": [Command.clear()]})
        with unittest.TestCase.assertRaises(self, (AccessError, UserError)):
            stale.submit_order(self.payload())

    def test_payload_currency_must_match_current_cny_source(self):
        message = self.assert_atomic_rejection(self.payload(currency="USD"), (UserError,))
        self.assertIn("currency", message.lower())

    def test_unit_price_must_be_exactly_representable_in_currency_rounding(self):
        for price in ("0.001", "12.345", "0.000001"):
            payload = self.payload()
            payload["lines"][0]["unit_price"] = price
            normalize_order_payload(payload)  # Valid transport precision; invalid CNY price.
            with self.subTest(price=price):
                self.assert_atomic_rejection(payload, (UserError,))
        payload = self.payload()
        payload["lines"][0]["unit_price"] = "12.500000"
        self.assertEqual(self.submit(payload)["state"], "pending")

    def test_discount_must_fit_actual_native_decimal_precision_without_rounding(self):
        precision = self.env["decimal.precision"].search([("name", "=", "Discount")], limit=1)
        self.assertTrue(precision)
        precision.write({"digits": 2})
        payload = self.payload()
        payload["lines"][0]["discount"] = "1.001"
        normalize_order_payload(payload)
        self.assert_atomic_rejection(payload, (UserError,))
        precision.write({"digits": 3})
        self.assertEqual(self.submit(payload)["state"], "pending")

    def test_precision_errors_use_original_request_line_index_despite_canonical_sort(self):
        precision = self.env["decimal.precision"].search([("name", "=", "Discount")], limit=1)
        self.assertTrue(precision)
        precision.write({"digits": 2})
        for field, value in (("unit_price", "0.001"), ("discount", "1.001")):
            payload = self.payload()
            self.assertEqual([line["external_line_id"] for line in payload["lines"]], ["2", "1"])
            payload["lines"][0][field] = value
            normalized = normalize_order_payload(payload)
            self.assertEqual(normalized.as_dict()["lines"][1]["external_line_id"], "2")
            with self.subTest(field=field):
                message = self.assert_atomic_rejection(payload, (UserError,))
                self.assertIn(f"lines[0].{field}", message)
                self.assertNotIn(f"lines[1].{field}", message)

    def test_fractional_quantity_is_retained_for_later_uom_validation(self):
        payload = self.payload()
        payload["lines"][0]["quantity"] = "0.000001"
        job = self.job(self.submit(payload))
        self.assertIn('"quantity":"0.000001"', job.canonical_payload)
        self.assertEqual(job.state, "pending")

    def test_all_declared_job_fields_are_readonly_and_company_links_checked(self):
        model = self.actor()
        for name in self.AUDIT:
            with self.subTest(field=name):
                self.assertTrue(model._fields[name].readonly)
        self.assertTrue(model._check_company_auto)
        self.assertTrue(model._fields["source_id"].check_company)
        self.assertTrue(model._fields["sale_order_id"].check_company)

    def test_public_create_write_unlink_copy_and_copy_data_are_always_denied(self):
        response = self.submit()
        original = self.job(response).read(list(self.AUDIT))
        forged = {
            "source_id": self.source_b.id, "company_id": self.company_b.id,
            "external_order_id": "FORGED", "payload_hash": "FORGED", "raw_payload": "FORGED",
            "canonical_payload": "FORGED", "submitted_by_id": self.settings.id,
            "received_at": fields.Datetime.now(), "state": "done", "attempt_count": 99,
            "last_attempt_at": fields.Datetime.now(), "error_code": "FORGED",
            "error_detail": "FORGED", "sale_order_id": False,
        }
        for user in (self.integration_a, self.operator_a, self.sales_a, self.settings, self.plain):
            for sudo in (False, True):
                model = self.actor(user=user).sudo(sudo)
                record = model.browse(response["job_id"])
                operations = (
                    lambda: model.create(copy.deepcopy(forged)),
                    lambda: model.create({}), lambda: record.write(forged),
                    lambda: record.write({}), lambda: record.unlink(),
                    lambda: record.copy(), lambda: record.copy_data(),
                )
                for operation in operations:
                    with self.subTest(user=user.login, sudo=sudo), self.assertRaises(AccessError):
                        operation()
        self.assertEqual(self.job(response).read(list(self.AUDIT)), original)

    def test_sales_and_ordinary_settings_have_no_job_read_acl(self):
        response = self.submit()
        for user in (self.sales_a, self.settings, self.plain):
            model = self.actor(user=user).sudo()
            for operation in (
                lambda: model.search([]), lambda: model.search_count([]),
                lambda: model.browse(response["job_id"]).read(["id"]),
                lambda: model.browse(response["job_id"]).web_read({"id": {}}),
                lambda: model.browse(response["job_id"]).export_data(["id"]),
            ):
                with self.subTest(user=user.login), self.assertRaises(AccessError):
                    operation()

    def test_integration_and_operator_job_reads_are_source_and_company_scoped(self):
        a = self.submit()
        b = self.submit(self.payload(self.source_b), user=self.integration_b)
        other = self.submit(self.payload(self.other_source), user=self.other_integration)
        for user in (self.integration_a, self.operator_a):
            model = self.actor(user=user).sudo()
            self.assertEqual(model.search([]).ids, [a["job_id"]])
            self.assertEqual(model.search_count([]), 1)
            self.assertEqual(model.browse(a["job_id"]).read(["raw_payload"])[0]["id"], a["job_id"])
            for response in (b, other):
                with self.subTest(user=user.login, job=response["job_id"]), self.assertRaises(AccessError):
                    model.browse(response["job_id"]).read(["id"])

    def test_every_public_read_and_query_path_rechecks_actual_actor(self):
        response = self.submit(self.payload(self.other_source), user=self.other_integration)
        model = self.actor().sudo()
        record = model.browse(response["job_id"])
        domain = [("id", "=", response["job_id"])]
        for operation in (
            lambda: record.read(["id"]), lambda: record.web_read({"id": {}}),
            lambda: record.web_read({"raw_payload": {}}), lambda: record.export_data(["id"]),
        ):
            with self.assertRaises(AccessError):
                operation()
        self.assertFalse(model.search(domain))
        self.assertFalse(model.search_fetch(domain, ["id"]))
        self.assertFalse(model.search_read(domain, ["id"]))
        self.assertEqual(model.search_count(domain), 0)
        self.assertFalse(model.read_group(domain, ["company_id"], ["company_id"]))
        self.assertFalse(model.name_search(args=domain))

    def test_payload_and_error_fields_metadata_is_limited_to_integration_and_operator(self):
        protected = ("raw_payload", "canonical_payload", "payload_hash", "error_code", "error_detail")
        for user in (self.integration_a, self.operator_a):
            self.assertTrue(set(protected) <= self.actor(user=user).fields_get().keys())
        for user in (self.sales_a, self.settings, self.plain):
            metadata = self.actor(user=user).sudo().fields_get()
            self.assertFalse(set(protected) & metadata.keys())

    def test_exists_rpc_is_private_without_altering_native_models(self):
        response = self.submit()
        for user in (self.integration_a, self.operator_a, self.sales_a, self.settings):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                get_public_method(self.actor(user=user).sudo(), "exists")
        self.assertEqual(self.job(response).exists().id, response["job_id"])
        self.assertTrue(callable(get_public_method(self.native["res.partner"], "exists")))

    def test_stale_job_recordset_cannot_read_after_source_authorization_removed(self):
        record = self.job(self.submit())
        record.read(["raw_payload"])
        self.source_a.write({"integration_user_ids": [Command.clear()]})
        for operation in (lambda: record.read(["id"]), lambda: record.web_read({"id": {}}),
                          lambda: record.export_data(["id"])):
            with self.assertRaises(AccessError):
                operation()
        self.assertFalse(self.actor().search([]))

    def test_stale_job_recordset_cannot_read_after_actual_company_membership_removed(self):
        response = self.submit(self.payload(self.source_b), user=self.integration_both)
        record = self.job(response, self.integration_both)
        record.read(["id"])
        self.integration_both.write({"company_ids": [Command.set(self.company_a.ids)]})
        with self.assertRaises(AccessError):
            record.read(["id"])
        with self.assertRaises(AccessError):
            record.web_read({"id": {}})

    def test_failed_submit_rolls_back_inside_callers_live_transaction(self):
        payload = self.payload()
        payload["lines"][0]["unit_price"] = "0.001"
        self.assert_atomic_rejection(payload, (UserError,))
        # The same transaction remains usable and the revision starts from zero.
        response = self.submit()
        self.assertEqual(response["state"], "pending")
        self.assertEqual(self.source_a.ingress_revision, 1)

    def test_database_enforces_company_source_external_order_unique_key(self):
        response = self.submit()
        self.env.flush_all()
        with mute_logger("odoo.sql_db"), unittest.TestCase.assertRaises(self, IntegrityError) as caught:
            with self.cr.savepoint():
                self.cr.execute("""
                    INSERT INTO portfolio_import_job (
                        source_id, company_id, external_order_id, payload_hash, raw_payload,
                        canonical_payload, submitted_by_id, received_at, state, attempt_count
                    ) SELECT source_id, company_id, external_order_id, payload_hash, raw_payload,
                             canonical_payload, submitted_by_id, received_at, state, attempt_count
                      FROM portfolio_import_job WHERE id = %s
                """, [response["job_id"]])
        self.assertEqual(caught.exception.diag.constraint_name,
                         "portfolio_import_job_order_identity_unique")

    def assert_private_source_helper_denied(self, model, source, helper):
        before = source.ingress_revision
        data = normalize_order_payload(self.payload(source)).as_dict()
        with self.cr.savepoint():
            with unittest.TestCase.assertRaises(self, AccessError):
                if helper == "lock":
                    model._ingress_lock(source.sudo())
                else:
                    model._ingress_configuration(source.sudo(), data)
            self.env.flush_all()
            self.env.invalidate_all()
            self.assertEqual(source.ingress_revision, before)

    def test_private_source_helpers_recheck_actual_role_including_retained_sudo(self):
        for user in (self.plain, self.settings, self.sales_a, self.operator_a):
            for sudo in (False, True):
                for helper in ("lock", "configuration"):
                    with self.subTest(user=user.login, sudo=sudo, helper=helper):
                        model = self.actor(user=user).sudo(sudo).with_context(
                            uid=self.integration_a.id, import_internal=True,
                        )
                        self.assert_private_source_helper_denied(model, self.source_a, helper)

    def test_private_source_helpers_reauthorize_source_and_actual_active_company(self):
        cases = (
            (self.actor().sudo(), self.other_source),
            (self.actor().sudo(), self.source_b),
            (self.actor(user=self.integration_both, companies=self.company_a).sudo(), self.source_b),
            (self.actor().sudo().with_context(allowed_company_ids=self.companies.ids), self.source_a),
        )
        for model, source in cases:
            for helper in ("lock", "configuration"):
                with self.subTest(uid=model.env.uid, source=source.id, helper=helper):
                    self.assert_private_source_helper_denied(model, source, helper)

    def test_private_source_helpers_reject_revoked_archived_or_inactive_actor(self):
        cases = (
            (self.source_a, {"integration_user_ids": [Command.clear()]}),
            (self.source_a, {"active": False}),
            (self.integration_a, {"active": False}),
        )
        for target, values in cases:
            with self.subTest(target=target._name, values=values), closing(self.cr.savepoint()):
                target.write(values)
                for helper in ("lock", "configuration"):
                    self.assert_private_source_helper_denied(self.actor().sudo(), self.source_a, helper)

    def test_private_source_helpers_require_membership_despite_settings_rule_union(self):
        privileged = self.make_user("settings_integration", f"{self.INTEGRATION},base.group_system")
        for helper in ("lock", "configuration"):
            with self.subTest(helper=helper):
                self.assert_private_source_helper_denied(
                    self.actor(user=privileged).sudo(), self.source_a, helper,
                )

    def test_authorized_private_source_helpers_and_operator_status_still_work(self):
        model = self.actor().sudo().with_context(default_state="done")
        model._ingress_configuration(
            self.source_a.sudo(), normalize_order_payload(self.payload()).as_dict(),
        )
        model._ingress_lock(self.source_a.sudo())
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.source_a.ingress_revision, 1)
        response = self.submit()
        for user in (self.integration_a, self.operator_a):
            result = self.job(response, user)._ingress_result(reused=True)
            self.assertEqual(result, {**response, "reused": True})

    def test_private_result_rechecks_job_acl_source_and_actual_company_under_sudo(self):
        response = self.submit()
        foreign = self.submit(self.payload(self.source_b), user=self.integration_both)
        other = self.submit(self.payload(self.other_source), user=self.other_integration)
        cases = [(self.actor(user=user).sudo(), response["job_id"])
                 for user in (self.plain, self.settings, self.sales_a)]
        cases.extend((self.actor(user=user).sudo(), other["job_id"])
                     for user in (self.integration_a, self.operator_a))
        cases.append((self.actor(user=self.integration_both, companies=self.company_a).sudo(),
                      foreign["job_id"]))
        for model, job_id in cases:
            with self.subTest(uid=model.env.uid, job=job_id), self.assertRaises(AccessError):
                model.browse(job_id)._ingress_result(reused=True)
        stale = self.job(response).sudo()
        stale._ingress_result(reused=True)
        self.source_a.write({"integration_user_ids": [Command.clear()]})
        with self.assertRaises(AccessError):
            stale._ingress_result(reused=True)
