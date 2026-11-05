"""The plain local M3 profile owns only its marked static acceptance data."""

import importlib
import json
import secrets
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from odoo import SUPERUSER_ID, Command
from odoo.addons.portfolio_trade.acceptance_data import (
    EXPECTED_IDENTITIES as M1_IDENTITIES,
)
from odoo.addons.portfolio_trade.acceptance_data import ROLE_GROUPS, prepare_acceptance
from odoo.addons.portfolio_trade.local_finance import prepare_local_finance
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from ..models.access import FORBIDDEN_GROUPS, ImportAccess

M1 = "portfolio_trade_acceptance"
FINANCE = "portfolio_trade_finance"
NAMESPACE = "portfolio_order_bridge_acceptance"
ROLES = ("settings", "operator", "integration")
SOURCE = "portfolio.import.source"
IDENTITIES = {
    **{f"user_{role}": "res.users" for role in ROLES},
    "customer": "res.partner", "vendor": "res.partner",
    **{f"product_item{index}": "product.product" for index in (1, 2)},
    **{f"template_item{index}": "product.template" for index in (1, 2)},
    **{f"supplier_item{index}": "product.supplierinfo" for index in (1, 2)},
    "source": SOURCE, "customer_map": "portfolio.import.customer.map",
    **{f"product_map_item{index}": "portfolio.import.product.map" for index in (1, 2)},
    "processing_cron": "ir.cron", "processing_server_action": "ir.actions.server",
}
USER_NAMES = {role: f"Portfolio bridge acceptance {role}" for role in ROLES}
PRODUCT_NAMES = {index: f"Portfolio bridge acceptance item {index}" for index in (1, 2)}


class _RollbackFixture(Exception):
    pass


@tagged("post_install", "-at_install", "portfolio_local_import_acceptance")
class TestLocalImportAcceptance(TransactionCase):
    """Separate fixture case: no inherited M1/M2 test methods are duplicated."""

    def setUp(self):
        super().setUp()
        helper = Path(__file__).resolve().parents[1] / "local_acceptance.py"
        self.assertTrue(helper.is_file(), "Plain local order bridge profile helper is missing")
        self.module = importlib.import_module("odoo.addons.portfolio_order_bridge.local_acceptance")
        self.assertTrue(callable(getattr(self.module, "prepare_local_import", None)))
        prepare_acceptance(
            self.env, {role: secrets.token_urlsafe(24) for role in ROLE_GROUPS}, allow_local=True,
        )
        self.company, self.other = self.owned("company_a", M1), self.owned("company_b", M1)
        self.native = self.env(context={"allowed_company_ids": self.company.ids})
        prepare_local_finance(self.native, allow_local=True)
        self.passwords = {role: secrets.token_urlsafe(32) for role in ROLES}
        # Native UID1 can be share=True. The helper may use it only for bounded
        # bootstrap maintenance, never to call guarded source/map services.
        self.maintainer = self.native["res.users"].with_context(no_reset_password=True).create({
            "name": "Local bridge test maintainer", "login": "local.bridge.test.maintainer",
            "company_id": self.company.id, "company_ids": [Command.set((self.company | self.other).ids)],
            "groups_id": [Command.set((self.env.ref("base.group_user") | self.env.ref("base.group_system")).ids)],
            "notification_type": "inbox", "email": "bridge-maintainer@example.invalid", "lang": "en_US",
        })

    def markers(self, namespace=NAMESPACE):
        return self.env["ir.model.data"].search([("module", "=", namespace)], order="name")

    def owned(self, key, namespace=NAMESPACE):
        marker = self.markers(namespace).filtered(lambda item: item.name == key)
        if not marker:
            return self.env["ir.model.data"]
        return self.env[marker.model].with_context(active_test=False).browse(marker.res_id).exists()

    def marker_facts(self, namespace=NAMESPACE):
        return tuple((item.name, item.model, item.res_id, item.noupdate) for item in self.markers(namespace))

    def prepare(self, **kwargs):
        return self.module.prepare_local_import(
            kwargs.pop("env", self.native), kwargs.pop("passwords", self.passwords),
            allow_local=kwargs.pop("allow_local", True), **kwargs,
        )

    def count(self, model):
        records = self.native[model].with_context(active_test=False)
        if isinstance(records, ImportAccess):
            # Test-only native count bypasses public configuration guards. The
            # production helper must call those guards using its new Settings.
            return super(ImportAccess, records).search_count([])
        return records.search_count([])

    def original_facts(self):
        self.env.flush_all()
        self.env.invalidate_all()
        users = tuple(
            (user.id, user.name, user.login, user.active, user.share, user.email,
             user.notification_type, user.lang, user.company_id.id,
             tuple(sorted(user.company_ids.ids)), tuple(sorted(user.groups_id.ids)), user.write_date)
            for user in (self.owned(f"user_{role}", M1) for role in ROLE_GROUPS) if user
        )
        companies = tuple(
            (company.id, company.name, company.currency_id.id, company.portfolio_masterdata_enabled,
             company.portfolio_approval_enabled, company.portfolio_fulfillment_enabled,
             company.portfolio_approval_policy_version, company.portfolio_stock_revision, company.write_date)
            for company in (self.company, self.other)
        )
        products = tuple(
            (product.id, product.default_code, product.name, product.company_id.id, product.write_date,
             product.with_company(self.company).qty_available, product.product_tmpl_id.write_date)
            for product in (self.owned(f"product_{key}", M1) for key in ("bearing", "sensor", "bolt")) if product
        )
        partners = tuple(
            (partner.id, partner.ref, partner.email, partner.company_id.id, partner.write_date,
             partner.with_company(self.company).property_product_pricelist.id)
            for partner in (self.owned(key, M1) for key in ("customer", "vendor")) if partner
        )
        globals_ = tuple((model, tuple((record.id, record.write_date) for record in
                                      self.env[model].with_context(active_test=False).search([], order="id")))
                         for model in ("res.groups", "res.currency", "ir.default", "product.pricelist"))
        return self.marker_facts(M1), self.marker_facts(FINANCE), users, companies, products, partners, globals_

    def live_snapshot(self):
        self.env.flush_all()
        self.env.invalidate_all()
        models = (
            "res.users", "res.partner", "res.company", "product.product", "product.template",
            "product.supplierinfo", "ir.cron", "ir.actions.server", "ir.model.data", "mail.mail",
            SOURCE, "portfolio.import.customer.map", "portfolio.import.product.map",
            "portfolio.import.job", "portfolio.import.attempt", "sale.order", "sale.order.line",
            "purchase.order", "account.move", "stock.move", "stock.quant", "stock.picking",
        )
        counts = tuple((model, self.count(model)) for model in models)
        own = tuple((key, record._name, record.id, record.write_date)
                    for key in IDENTITIES if (record := self.owned(key)))
        return counts, self.marker_facts(), self.original_facts(), own

    def assert_atomic_denied(self, operation, exception=UserError):
        # unittest's assertion supplies no rollback: a caught failure must
        # already have rolled back the helper's own complete savepoint.
        with self.cr.savepoint():
            before = self.live_snapshot()
            with unittest.TestCase.assertRaises(self, exception):
                operation()
            self.assertEqual(self.live_snapshot(), before)

    @contextmanager
    def mutation(self):
        try:
            with self.cr.savepoint():
                yield
                raise _RollbackFixture()
        except _RollbackFixture:
            self.env.registry.clear_cache()
            self.env.invalidate_all()

    def settings(self, model=SOURCE):
        return self.native[model].with_user(self.owned("user_settings")).with_context(
            allowed_company_ids=self.company.ids,
        )

    def source(self):
        return self.settings().browse(self.owned("source").id)

    def test_first_profile_creates_exact_owned_catalog_and_preserves_original_facts(self):
        original = self.original_facts()
        counts = dict(self.live_snapshot()[0])
        result = self.prepare()
        self.assertEqual(self.module.EXPECTED_IDENTITIES, IDENTITIES)
        self.assertEqual({marker.name: marker.model for marker in self.markers()}, IDENTITIES)
        self.assertEqual(len(self.markers()), len(IDENTITIES))
        self.assertTrue(all(self.markers().mapped("noupdate")))
        self.assertEqual(self.original_facts(), original)
        after = dict(self.live_snapshot()[0])
        self.assertEqual(after["res.users"] - counts["res.users"], 3)
        for model in ("sale.order", "sale.order.line", "purchase.order", "account.move", "stock.move",
                      "stock.quant", "stock.picking", "mail.mail", "portfolio.import.job", "portfolio.import.attempt"):
            self.assertEqual(after[model], counts[model], model)
        self.assertEqual(result["source_id"], self.owned("source").id)
        for index in (1, 2):
            product = self.owned(f"product_item{index}")
            supplier = self.owned(f"supplier_item{index}")
            self.assertEqual(product.product_tmpl_id, self.owned(f"template_item{index}"))
            self.assertEqual(product.name, PRODUCT_NAMES[index])
            self.assertEqual(product.default_code, f"BRIDGE-ACCEPTANCE-{index}")
            self.assertEqual(product.company_id, self.company)
            self.assertEqual(product.product_tmpl_id.company_id, self.company)
            self.assertTrue(product.active and product.is_storable)
            self.assertEqual(product.invoice_policy, "delivery")
            self.assertEqual(product.uom_id, self.env.ref("uom.product_uom_unit"))
            self.assertEqual(product.uom_po_id, product.uom_id)
            self.assertFalse(product.taxes_id | product.supplier_taxes_id)
            self.assertEqual(product.with_company(self.company).qty_available, 0)
            self.assertEqual(supplier.company_id, self.company)
            self.assertEqual(supplier.product_id, product)
            self.assertEqual(supplier.product_tmpl_id, product.product_tmpl_id)
            self.assertEqual(supplier.partner_id, self.owned("vendor"))
            self.assertEqual(supplier.currency_id, self.company.currency_id)
            self.assertGreater(supplier.price, 0)
        for key, ref in (("customer", "BRIDGE-ACCEPTANCE-CUSTOMER"),
                         ("vendor", "BRIDGE-ACCEPTANCE-VENDOR")):
            partner = self.owned(key)
            self.assertEqual(partner.company_id, self.company)
            self.assertEqual(partner.ref, ref)
            self.assertTrue(partner.active)
            self.assertTrue(not partner.email or partner.email.endswith(".invalid"))

    def test_three_new_ordinary_actors_have_exact_required_role_boundaries(self):
        self.prepare()
        for role in ROLES:
            user = self.owned(f"user_{role}")
            self.assertNotEqual(user.id, 1)
            self.assertTrue(user.active)
            self.assertFalse(user.share)
            self.assertEqual(user.name, USER_NAMES[role])
            self.assertEqual(user.login, f"portfolio.bridge.acceptance.{role}")
            self.assertTrue(user.email.endswith(".invalid"))
            self.assertEqual(user.notification_type, "inbox")
            self.assertEqual(user.lang, "en_US")
            self.assertEqual(user.company_id, self.company)
            self.assertEqual(user.company_ids, self.company)
            requested = self.env.ref("base.group_user") | self.env.ref({
                "settings": "base.group_system", "operator": "portfolio_order_bridge.group_import_operator",
                "integration": "portfolio_order_bridge.group_import_integration",
            }[role])
            if role == "operator":
                requested |= self.env.ref("sales_team.group_sale_salesman")
            expected = (requested | requested.trans_implied_ids) - self.env.ref("base.group_multi_company")
            expected |= self.env.ref("mail.group_mail_notification_type_inbox")
            self.assertEqual(set(user.groups_id.ids), set(expected.ids))
            self.assertTrue(user.has_group("base.group_user"))
            self.assertEqual(user.has_group("base.group_system"), role == "settings")
            self.assertEqual(user.has_group("portfolio_order_bridge.group_import_operator"), role == "operator")
            self.assertEqual(user.has_group("portfolio_order_bridge.group_import_integration"), role == "integration")
            self.assertEqual(user.has_group("sales_team.group_sale_salesman"), role == "operator")
            if role != "settings":
                for group in FORBIDDEN_GROUPS:
                    self.assertFalse(user.has_group(group), (role, group))
            auth = user.with_user(user)._check_credentials(
                {"type": "password", "password": self.passwords[role]}, {"interactive": True},
            )
            self.assertEqual(auth["uid"], user.id)
        with self.assertRaises(AccessError):
            self.native[SOURCE].search([])

    def test_source_maps_and_inactive_schedule_use_real_new_settings_and_native_services(self):
        self.assertEqual(self.native.uid, SUPERUSER_ID)
        self.assertTrue(self.native.su)
        self.assertFalse(self.native.user.active)  # Upstream's technical __system__ bootstrap.
        seen = []
        source_type = type(self.native[SOURCE])
        original_create, original_cron = source_type.create, source_type.action_prepare_processing_cron

        def assert_settings(records):
            self.assertNotEqual(records.env.uid, 1)
            self.assertFalse(records.env.su)
            self.assertEqual(records.env.user.login, "portfolio.bridge.acceptance.settings")
            self.assertEqual(records.env.companies, self.company)
            seen.append(records.env.uid)

        def create(records, values):
            assert_settings(records)
            return original_create(records, values)

        def cron(records):
            assert_settings(records)
            return original_cron(records)

        with patch.object(source_type, "create", create), \
                patch.object(source_type, "action_prepare_processing_cron", cron):
            self.prepare()
        self.assertGreaterEqual(len(seen), 2)
        self.assertFalse(self.native.user.active)
        source = self.source()
        self.assertEqual(source.create_uid, self.owned("user_settings"))
        self.assertEqual((source.company_code, source.code), ("PARTS-A", "B2B-ACCEPTANCE"))
        self.assertEqual(source.company_id, self.company)
        self.assertEqual(source.sales_user_id, self.owned("user_sales", M1))
        self.assertEqual(source.processor_user_id, self.owned("user_operator"))
        self.assertEqual(source.integration_user_ids, self.owned("user_integration"))
        self.assertEqual(source.warehouse_id, self.owned("warehouse_a", M1))
        self.assertEqual(source.pricelist_id, self.owned("pricelist_cny", FINANCE))
        self.assertEqual(source.ingress_revision, 0)
        for key in ("customer_map", "product_map_item1", "product_map_item2"):
            mapping = self.settings(IDENTITIES[key]).browse(self.owned(key).id)
            self.assertEqual(mapping.create_uid, self.owned("user_settings"))
            self.assertEqual(mapping.source_id, source)
            self.assertEqual(mapping.company_id, self.company)
            self.assertTrue(mapping.active)
        customer = self.settings("portfolio.import.customer.map").browse(self.owned("customer_map").id)
        self.assertEqual(customer.partner_id, self.owned("customer"))
        self.assertEqual(customer.external_ref, "BRIDGE-ACCEPTANCE-CUSTOMER")
        for index in (1, 2):
            mapping = self.settings("portfolio.import.product.map").browse(self.owned(f"product_map_item{index}").id)
            self.assertEqual(mapping.external_sku, f"BRIDGE-ACCEPTANCE-{index}")
            self.assertEqual(mapping.product_id, self.owned(f"product_item{index}"))
        cron = self.owned("processing_cron")
        self.assertEqual(source.processing_cron_id, cron)
        self.assertEqual(cron.ir_actions_server_id, self.owned("processing_server_action"))
        self.assertFalse(cron.active)
        self.assertEqual(cron.user_id, self.owned("user_operator"))
        self.assertEqual(cron.model_id.model, "portfolio.import.job")
        self.assertEqual((cron.code, cron.state), ("model._process_pending()", "code"))
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "minutes"))
        source._ui_validate_processing_cron(cron)

    def test_stable_safe_report_and_rerun_are_read_only_without_hash_upgrade(self):
        first = self.prepare()
        self.assertEqual(set(first), {
            "schema_version", "database", "namespace", "company_id", "source_id", "identities",
            "cron_active", "borrowed_m1_identities", "borrowed_finance_pricelist_id",
        })
        self.assertEqual((first["schema_version"], first["database"], first["namespace"]),
                         (1, self.cr.dbname, NAMESPACE))
        self.assertEqual(first["company_id"], self.company.id)
        self.assertIs(first["cron_active"], False)
        self.assertEqual(first["identities"], {
            key: {"model": model, "id": self.owned(key).id} for key, model in IDENTITIES.items()
        })
        self.assertEqual(first["borrowed_m1_identities"], {
            key: {"model": model, "id": self.owned(key, M1).id} for key, model in M1_IDENTITIES.items()
        })
        self.assertEqual(first["borrowed_finance_pricelist_id"], self.owned("pricelist_cny", FINANCE).id)
        before = self.live_snapshot()
        with patch.object(type(self.env["res.users"]), "_set_encrypted_password",
                          side_effect=AssertionError("Reruns must not upgrade or reset passwords")):
            second = self.prepare()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(self.live_snapshot(), before)
        output = json.dumps(first).lower()
        for forbidden in ("password", "credential", "db_host", "db_user", "dsn", "session", "cookie", "login"):
            self.assertNotIn(forbidden, output)
        for password in self.passwords.values():
            self.assertNotIn(password, output)

    def test_helper_requires_exact_opt_in_and_three_valid_distinct_passwords(self):
        for flag in (False, None, 1, "true"):
            with self.subTest(flag=flag):
                self.assert_atomic_denied(lambda: self.prepare(allow_local=flag))
        self.assert_atomic_denied(lambda: self.module.prepare_local_import(self.native, self.passwords))
        for passwords in ({}, {"settings": "x" * 32}, {**self.passwords, "sales": "z" * 32},
                          {**self.passwords, "operator": "short"}, {**self.passwords, "operator": " " * 32},
                          {**self.passwords, "operator": self.passwords["settings"]},
                          {**self.passwords, "operator": 1234}):
            with self.subTest(keys=tuple(passwords)):
                self.assert_atomic_denied(lambda: self.prepare(passwords=passwords))

    def target_proxy(self, database, host):
        actual = self.native

        class TargetEnvironment:
            cr = SimpleNamespace(dbname=database, _cnx=SimpleNamespace(info=SimpleNamespace(host=host)))
            uid, user, context = actual.uid, actual.user, actual.context

            def __call__(self, **kwargs):
                return actual(**kwargs)

            def __getitem__(self, name):
                return actual[name]

        return TargetEnvironment()

    def test_live_connection_host_and_database_override_context_or_config_claims(self):
        for database, host in (("production", "127.0.0.1"), (self.cr.dbname, "remote.invalid"),
                               (self.cr.dbname, "localhost"), (self.cr.dbname, "::1"), (self.cr.dbname, None)):
            with self.subTest(database=database, host=host):
                self.assert_atomic_denied(lambda: self.prepare(env=self.target_proxy(database, host)))

    def test_sudo_cannot_make_original_sales_an_actual_settings_actor(self):
        forged = self.native["res.users"].with_user(self.owned("user_sales", M1)).sudo().env
        self.assert_atomic_denied(lambda: self.prepare(env=forged), AccessError)

    def test_bootstrap_actor_must_already_own_and_activate_company_a(self):
        for values, companies in (({"company_ids": [Command.set(self.other.ids)], "company_id": self.other.id}, self.other),
                                  ({}, self.other), ({"active": False}, self.company)):
            with self.mutation():
                self.maintainer.write(values)
                forged = self.native["res.users"].with_user(self.maintainer).sudo().with_context(
                    allowed_company_ids=companies.ids,
                ).env
                self.assert_atomic_denied(lambda: self.prepare(env=forged), AccessError)

    def test_installed_module_state_and_source_version_must_be_ready_before_writes(self):
        module = self.env["ir.module.module"].search([("name", "=", "portfolio_order_bridge")])
        for values in ({"state": "uninstalled"}, {"latest_version": "18.0.0.0.0"}):
            with self.subTest(values=values), self.mutation():
                module.write(values)
                self.assert_atomic_denied(lambda: self.prepare())

    def test_missing_installed_model_or_view_metadata_is_rejected_before_writes(self):
        for name in ("model_portfolio_import_source", "view_import_source_form", "view_import_job_form"):
            with self.subTest(marker=name), self.mutation():
                marker = self.env["ir.model.data"].search([
                    ("module", "=", "portfolio_order_bridge"), ("name", "=", name),
                ])
                self.assertEqual(len(marker), 1)
                marker.unlink()
                self.assert_atomic_denied(lambda: self.prepare())

    def test_missing_runtime_field_is_rejected_before_bootstrap_writes(self):
        source_type = type(self.native[SOURCE])
        fields = {key: value for key, value in source_type._fields.items() if key != "processing_cron_id"}
        before = self.live_snapshot()
        with patch.object(source_type, "_fields", fields):
            with unittest.TestCase.assertRaises(self, UserError):
                self.prepare()
        self.assertEqual(self.live_snapshot(), before)

    def test_missing_native_table_catalog_read_rejects_without_schema_changes(self):
        original = type(self.cr).execute

        def execute(cursor, query, *args, **kwargs):
            if "pg_class" in str(query):
                return original(cursor, "SELECT 0")
            return original(cursor, query, *args, **kwargs)

        with patch.object(type(self.cr), "execute", execute):
            self.assert_atomic_denied(lambda: self.prepare())

    def test_missing_m1_or_finance_marker_never_seeds_or_repairs_prerequisites(self):
        for namespace, key in ((M1, "company_a"), (M1, "company_b"), (M1, "warehouse_a"),
                               (M1, "user_sales"), (M1, "initial_bearing_move"), (M1, "product_sensor"),
                               (FINANCE, "pricelist_cny")):
            with self.subTest(namespace=namespace, key=key), self.mutation():
                self.markers(namespace).filtered(lambda item: item.name == key).unlink()
                self.assert_atomic_denied(lambda: self.prepare())

    def test_invalid_borrowed_company_currency_warehouse_or_pricelist_is_rejected(self):
        for record, values in ((self.company, {"currency_id": self.env.ref("base.USD").id}),
                               (self.owned("pricelist_cny", FINANCE), {"active": False}),
                               (self.owned("pricelist_cny", FINANCE), {"currency_id": self.env.ref("base.USD").id})):
            with self.subTest(model=record._name, values=values), self.mutation():
                record.write(values)
                self.assert_atomic_denied(lambda: self.prepare())
        with self.mutation():
            both = self.env(context={"allowed_company_ids": (self.company | self.other).ids})
            foreign = both["stock.warehouse"].search([("company_id", "=", self.other.id)], limit=1)
            if not foreign:
                foreign = both["stock.warehouse"].create({
                    "name": "Unrelated company-B warehouse", "code": "BRTB", "company_id": self.other.id,
                })
            self.markers(M1).filtered(lambda item: item.name == "warehouse_a").write({"res_id": foreign.id})
            self.assert_atomic_denied(lambda: self.prepare())

    def test_unknown_native_login_name_partner_ref_or_sku_conflicts_are_not_adopted(self):
        conflicts = (
            ("res.users", {"name": "Unknown external user", "login": "portfolio.bridge.acceptance.operator"}),
            ("res.users", {"name": USER_NAMES["settings"], "login": "unknown.bridge.user"}),
            ("res.partner", {"name": "Unknown external partner", "ref": "BRIDGE-ACCEPTANCE-CUSTOMER"}),
            ("product.product", {"name": "Unknown external item", "default_code": "BRIDGE-ACCEPTANCE-1"}),
            ("product.product", {"name": PRODUCT_NAMES[2], "default_code": "UNKNOWN-BRIDGE-2"}),
        )
        for model, values in conflicts:
            with self.subTest(model=model, values=values), self.mutation():
                self.native[model].with_context(no_reset_password=True).create(values)
                self.assert_atomic_denied(lambda: self.prepare())

    def test_unknown_same_source_identity_rolls_back_all_bootstrap_users(self):
        self.prepare()
        source = self.source()
        # Make only the existing source unmarked. All other former profile
        # objects have different keys, so the source must still be detected by
        # the newly created Settings actor inside the helper's savepoint.
        self.markers().unlink()
        for role in ROLES:
            user = self.env["res.users"].search([("login", "=", f"portfolio.bridge.acceptance.{role}")])
            user.write({"login": f"unmarked.bridge.{role}", "name": f"Unmarked bridge {role}"})
        for index in (1, 2):
            self.native["product.product"].search([("default_code", "=", f"BRIDGE-ACCEPTANCE-{index}")]).write({
                "name": f"Unmarked bridge item {index}", "default_code": f"UNMARKED-BRIDGE-{index}",
            })
        self.native["res.partner"].search([("ref", "in", ["BRIDGE-ACCEPTANCE-CUSTOMER", "BRIDGE-ACCEPTANCE-VENDOR"])]).write({
            "name": "Unmarked bridge partner", "ref": False,
        })
        self.assertEqual(source.code, "B2B-ACCEPTANCE")
        self.assert_atomic_denied(lambda: self.prepare())
        self.assertFalse(self.markers())

    def test_established_namespace_missing_wrong_model_stale_extra_and_duplicate_targets_conflict(self):
        self.prepare()
        operations = (
            lambda: self.markers().filtered(lambda item: item.name == "product_item2").unlink(),
            lambda: self.markers().filtered(lambda item: item.name == "customer").write({"model": "res.company", "res_id": self.company.id}),
            lambda: self.markers().filtered(lambda item: item.name == "customer").write({"res_id": 2147483647}),
            lambda: self.markers().filtered(lambda item: item.name == "vendor").write({"res_id": self.owned("customer").id}),
            lambda: self.env["ir.model.data"].create({"module": NAMESPACE, "name": "extra", "model": "res.company", "res_id": self.company.id}),
            lambda: self.markers().filtered(lambda item: item.name == "processing_server_action").write({"noupdate": False}),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.mutation():
                operation()
                self.assert_atomic_denied(lambda: self.prepare())

    def test_modified_owned_users_products_partners_and_supplier_are_never_repaired(self):
        self.prepare()
        alterations = (
            ("user_operator", {"name": "User edited operator"}),
            ("user_operator", {"email": "user-changed@example.invalid"}),
            ("user_operator", {"notification_type": "email"}),
            ("user_operator", {"groups_id": [Command.link(self.env.ref("sales_team.group_sale_salesman_all_leads").id)]}),
            ("user_integration", {"company_ids": [Command.link(self.other.id)]}),
            ("customer", {"ref": "USER-CHANGED-CUSTOMER"}),
            ("product_item1", {"default_code": "USER-CHANGED-SKU"}),
            ("product_item2", {"active": False}),
            ("product_item1", {"invoice_policy": "order"}),
            ("supplier_item1", {"price": 99999}),
            ("supplier_item2", {"currency_id": self.env.ref("base.USD").id}),
        )
        for key, values in alterations:
            with self.subTest(key=key, values=values), self.mutation():
                self.owned(key).write(values)
                self.assert_atomic_denied(lambda: self.prepare())

    def test_incorrect_existing_passwords_conflict_without_resetting_accounts(self):
        self.prepare()
        self.assert_atomic_denied(lambda: self.prepare(passwords={**self.passwords, "operator": "different-private-password-" + "x" * 32}))
        with self.mutation():
            self.owned("user_operator").write({"password": "user-changed-private-password-" + "x" * 32})
            self.assert_atomic_denied(lambda: self.prepare())

    def test_modified_source_mapping_or_cron_conflicts_without_overwrite_or_enable(self):
        self.prepare()
        alterations = (
            ("source", {"name": "User edited source"}),
            ("source", {"active": False}),
            ("customer_map", {"external_ref": "USER-CHANGED-CUSTOMER"}),
            ("product_map_item1", {"product_id": self.owned("product_item2").id}),
            ("product_map_item2", {"active": False}),
            ("processing_cron", {"active": True}),
            ("processing_cron", {"code": "model.search([])"}),
            ("processing_cron", {"interval_number": 2}),
        )
        for key, values in alterations:
            with self.subTest(key=key, values=values), self.mutation():
                if key in {"source", "customer_map", "product_map_item1", "product_map_item2"}:
                    self.settings(IDENTITIES[key]).browse(self.owned(key).id).write(values)
                else:
                    self.owned(key).with_user(self.owned("user_settings")).write(values)
                self.assert_atomic_denied(lambda: self.prepare())

    def test_late_public_cron_failure_rolls_back_all_owned_records_and_never_commits(self):
        source_type = type(self.native[SOURCE])
        original = source_type.action_prepare_processing_cron

        def fail_after_cron(records):
            original(records)
            self.assertTrue(records.processing_cron_id)
            raise UserError("Injected late local bridge failure")

        with patch.object(source_type, "action_prepare_processing_cron", fail_after_cron), \
                patch.object(type(self.cr), "commit", side_effect=AssertionError("Helper must not commit")):
            self.assert_atomic_denied(lambda: self.prepare())
        self.assertFalse(self.markers())

    def test_rerun_does_not_call_any_configuration_create_write_or_cron_action(self):
        self.prepare()
        before = self.live_snapshot()
        source_type = type(self.native[SOURCE])
        with patch.object(source_type, "create", side_effect=AssertionError("Read-only rerun created a source")), \
                patch.object(source_type, "write", side_effect=AssertionError("Read-only rerun changed a source")), \
                patch.object(source_type, "action_prepare_processing_cron", side_effect=AssertionError("Read-only rerun invoked a write action")), \
                patch.object(type(self.env["res.users"]), "write", side_effect=AssertionError("Read-only rerun changed users")), \
                patch.object(type(self.cr), "commit", side_effect=AssertionError("Helper must not commit")):
            self.prepare()
        self.assertEqual(self.live_snapshot(), before)
