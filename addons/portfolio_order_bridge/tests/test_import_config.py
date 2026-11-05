import unittest
from contextlib import closing, contextmanager

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.service.model import get_public_method
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger
from psycopg2 import IntegrityError


@tagged("post_install", "-at_install", "portfolio_import_config")
class TestImportConfig(TransactionCase):
    """Real ORM contract for configuration; all fixtures roll back with the case."""

    SOURCE = "portfolio.import.source"
    CUSTOMER = "portfolio.import.customer.map"
    PRODUCT = "portfolio.import.product.map"
    INTEGRATION = "portfolio_order_bridge.group_import_integration"
    OPERATOR = "portfolio_order_bridge.group_import_operator"
    SALES = "sales_team.group_sale_salesman"
    FORBIDDEN = (
        "base.group_system", "base.group_erp_manager", "sales_team.group_sale_manager",
        "sales_team.group_sale_salesman_all_leads",
        "purchase.group_purchase_user", "stock.group_stock_user", "account.group_account_invoice",
    )
    PRIVATE_FIELDS = (
        "pricelist_id", "warehouse_id", "sales_user_id", "processor_user_id",
        "integration_user_ids",
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cny = cls.env.ref("base.CNY")
        cls.usd = cls.env.ref("base.USD")
        cls.company_a = cls.env["res.company"].create({
            "name": "Import isolated company A", "currency_id": cls.cny.id,
        })
        cls.company_b = cls.env["res.company"].create({
            "name": "Import isolated company B", "currency_id": cls.cny.id,
        })
        cls.companies = cls.company_a | cls.company_b
        cls.native = cls.env(context={**cls.env.context, "allowed_company_ids": cls.companies.ids})
        cls.warehouse_a = cls.native["stock.warehouse"].search([
            ("company_id", "=", cls.company_a.id),
        ], limit=1)
        cls.warehouse_b = cls.native["stock.warehouse"].search([
            ("company_id", "=", cls.company_b.id),
        ], limit=1)
        for suffix, company in (("a", cls.company_a), ("b", cls.company_b)):
            if not getattr(cls, f"warehouse_{suffix}"):
                setattr(cls, f"warehouse_{suffix}", cls.native["stock.warehouse"].create({
                    "name": f"Import warehouse {suffix}", "code": f"IM{suffix.upper()}",
                    "company_id": company.id,
                }))
            setattr(cls, f"pricelist_{suffix}", cls.native["product.pricelist"].create({
                "name": f"Import CNY {suffix}", "currency_id": cls.cny.id,
                "company_id": company.id,
            }))
            setattr(cls, f"partner_{suffix}", cls.native["res.partner"].create({
                "name": f"Import customer {suffix}", "company_id": company.id,
            }))
            setattr(cls, f"product_{suffix}", cls.native["product.product"].create({
                "name": f"Import product {suffix}", "type": "service", "company_id": company.id,
            }))
            setattr(cls, f"sales_{suffix}", new_test_user(
                cls.env, login=f"import_sales_{suffix}", groups=cls.SALES,
                company_id=company.id, company_ids=[Command.set(company.ids)],
            ))
        cls.shared_pricelist = cls.native["product.pricelist"].create({
            "name": "Import shared CNY", "currency_id": cls.cny.id, "company_id": False,
        })
        cls.shared_partner = cls.native["res.partner"].create({
            "name": "Import shared customer", "company_id": False,
        })
        cls.shared_product = cls.native["product.product"].create({
            "name": "Import shared product", "type": "service", "company_id": False,
        })
        cls.settings = new_test_user(
            cls.env, login="import_settings", groups="base.group_system",
            company_id=cls.company_a.id, company_ids=[Command.set(cls.companies.ids)],
        )
        cls.settings_a = new_test_user(
            cls.env, login="import_settings_a", groups="base.group_system",
            company_id=cls.company_a.id, company_ids=[Command.set(cls.company_a.ids)],
        )

    def setUp(self):
        super().setUp()
        for name in (self.SOURCE, self.CUSTOMER, self.PRODUCT):
            self.assertTrue(name in self.env.registry, f"M3 source/mapping model missing: {name}")
        self.sequence = 0
        for suffix, company in (("a", self.company_a), ("b", self.company_b)):
            setattr(self, f"operator_{suffix}", self.user(
                f"operator_{suffix}", f"{self.OPERATOR},{self.SALES}", company,
            ))
            setattr(self, f"integration_{suffix}", self.user(
                f"integration_{suffix}", self.INTEGRATION, company,
            ))
        self.other_operator = self.user("other_operator", f"{self.OPERATOR},{self.SALES}")
        self.other_integration = self.user("other_integration", self.INTEGRATION)
        self.source_a = self.source()
        self.source_b = self.source(company=self.company_b)
        self.other_source = self.source(processor_user_id=self.other_operator.id,
                                        integration_user_ids=[Command.set(self.other_integration.ids)])
        self.customer_map = self.customer_mapping(self.source_a)
        self.product_map = self.product_mapping(self.source_a)
        self.customer_map_b = self.customer_mapping(self.source_b)
        self.product_map_b = self.product_mapping(self.source_b)

    def user(self, name, groups="base.group_user", company=None, companies=None, **values):
        company = company or self.company_a
        return new_test_user(
            self.env, login=f"import_{name}", groups=groups, company_id=company.id,
            context={"no_reset_password": True},
            company_ids=[Command.set((companies or company).ids)], **values,
        )

    def actor(self, model, user=None, companies=None):
        user = user or self.settings
        return self.env[model].with_user(user).with_context(
            allowed_company_ids=(companies or user.company_ids).ids,
        )

    def source_values(self, company=None, **values):
        self.sequence += 1
        company = company or self.company_a
        suffix = "a" if company == self.company_a else "b"
        return {
            "name": f"Import source {self.sequence}", "company_id": company.id,
            "company_code": f"IMPORT-{suffix.upper()}", "code": f"SOURCE-{self.sequence}",
            "pricelist_id": getattr(self, f"pricelist_{suffix}").id,
            "warehouse_id": getattr(self, f"warehouse_{suffix}").id,
            "sales_user_id": getattr(self, f"sales_{suffix}").id,
            "processor_user_id": getattr(self, f"operator_{suffix}").id,
            "integration_user_ids": [Command.set(getattr(self, f"integration_{suffix}").ids)],
            **values,
        }

    def source(self, company=None, **values):
        return self.actor(self.SOURCE).create(self.source_values(company, **values))

    def customer_mapping(self, source, **values):
        suffix = "a" if source.company_id == self.company_a else "b"
        return self.actor(self.CUSTOMER).create({
            "source_id": source.id, "external_ref": "CUSTOMER-1",
            "partner_id": getattr(self, f"partner_{suffix}").id, **values,
        })

    def product_mapping(self, source, **values):
        suffix = "a" if source.company_id == self.company_a else "b"
        return self.actor(self.PRODUCT).create({
            "source_id": source.id, "external_sku": "SKU-1",
            "product_id": getattr(self, f"product_{suffix}").id, **values,
        })

    @contextmanager
    def rejects(self, exceptions):
        # Odoo's assertRaises only accepts one class; its issubclass check rejects tuples.
        with unittest.TestCase.assertRaises(self, exceptions), self.cr.savepoint():
            yield

    def test_fields_and_valid_company_and_shared_targets(self):
        self.assertTrue(self.source_a._check_company_auto)
        expected = {"name", "active", "company_id", "company_code", "code", *self.PRIVATE_FIELDS}
        self.assertTrue(expected <= set(self.source_a._fields))
        self.assertEqual(self.source_a.company_id, self.company_a)
        self.source_a._validate_configuration()
        self.source_a.write({"pricelist_id": self.shared_pricelist.id})
        for mapping, field, target in (
            (self.customer_map, "partner_id", self.shared_partner),
            (self.product_map, "product_id", self.shared_product),
        ):
            self.assertEqual(mapping.company_id, self.company_a)
            self.assertTrue(mapping._fields["company_id"].store)
            self.assertEqual(mapping._fields["company_id"].related, "source_id.company_id")
            self.assertTrue(mapping._fields[field].check_company)
            mapping.write({field: target.id})
            mapping._validate_target()
            self.assertEqual(mapping[field], target)

    def test_groups_only_imply_internal_user(self):
        for xmlid in (self.INTEGRATION, self.OPERATOR):
            self.assertEqual(self.env.ref(xmlid).implied_ids, self.env.ref("base.group_user"))
        self.assertFalse(self.integration_a.has_group(self.SALES))
        self.assertFalse(self.integration_a.has_group("purchase.group_purchase_user"))
        self.assertFalse(self.integration_a.has_group("stock.group_stock_user"))

    def test_codes_reject_empty_whitespace_controls_surrogates_and_overflow(self):
        invalid = (False, "", " ", " x", "x ", "x\n", "a\x00b", "a\x7fb", "a\ud800b", "x" * 65)
        for field in ("company_code", "code"):
            for value in invalid:
                with self.subTest(field=field, value=repr(value)), self.cr.savepoint():
                    with self.rejects((ValidationError, UserError)):
                        self.source(**{field: value})
        for make, key in ((self.customer_mapping, "external_ref"), (self.product_mapping, "external_sku")):
            for value in invalid:
                with self.subTest(field=key, value=repr(value)), self.cr.savepoint():
                    with self.rejects((ValidationError, UserError)):
                        make(self.source_a, **{key: value})
        accepted = self.source(company_code="公" * 64, code="S" * 64)
        self.customer_mapping(accepted, external_ref="C" * 64)
        self.product_mapping(accepted, external_sku="P" * 64)

    def test_name_is_nonempty_clean_safe_and_bounded(self):
        for value in (False, "", " ", " x", "x ", "x\n", "a\x00b", "a\udfff", "x" * 129):
            with self.subTest(value=repr(value)), self.cr.savepoint():
                with self.rejects((ValidationError, UserError)):
                    self.source(name=value)
        self.assertEqual(self.source(name="名" * 128).name, "名" * 128)

    def test_source_uniqueness_is_global_and_includes_archived(self):
        self.source_a.write({"active": False})
        for company, external_company in (
            (self.company_a, "OTHER-COMPANY-CODE"),
            (self.company_b, self.source_a.company_code),
        ):
            with self.subTest(company=company.id), self.cr.savepoint(), mute_logger("odoo.sql_db"):
                with self.rejects((ValidationError, UserError, IntegrityError)):
                    self.source(company=company, company_code=external_company, code=self.source_a.code)
        self.source(company=self.company_b, code=self.source_a.code)

    def test_mapping_unique_per_source_even_when_archived(self):
        for mapping, create in ((self.customer_map, self.customer_mapping), (self.product_map, self.product_mapping)):
            mapping.write({"active": False})
            with self.cr.savepoint(), mute_logger("odoo.sql_db"), self.rejects((ValidationError, UserError, IntegrityError)):
                create(self.source_a)
            self.assertTrue(create(self.other_source))

    def test_unique_keys_are_enforced_by_database_after_archive(self):
        self.source_a.write({"active": False})
        self.customer_map.write({"active": False})
        self.product_map.write({"active": False})
        self.env.flush_all()
        # Bypass ORM validation to establish that races still meet a database constraint.
        statements = (
            (
                """INSERT INTO portfolio_import_source
                    (name, active, company_id, company_code, code, pricelist_id,
                     warehouse_id, sales_user_id, processor_user_id)
                    SELECT name, active, company_id, 'DIFFERENT-COMPANY-CODE', code,
                           pricelist_id, warehouse_id, sales_user_id, processor_user_id
                    FROM portfolio_import_source WHERE id = %s""",
                [self.source_a.id],
            ),
            (
                """INSERT INTO portfolio_import_source
                    (name, active, company_id, company_code, code, pricelist_id,
                     warehouse_id, sales_user_id, processor_user_id)
                    SELECT name, active, %s, company_code, code, pricelist_id,
                           warehouse_id, sales_user_id, processor_user_id
                    FROM portfolio_import_source WHERE id = %s""",
                [self.company_b.id, self.source_a.id],
            ),
            (
                """INSERT INTO portfolio_import_customer_map
                    (active, source_id, company_id, external_ref, partner_id)
                    SELECT active, source_id, company_id, external_ref, partner_id
                    FROM portfolio_import_customer_map WHERE id = %s""",
                [self.customer_map.id],
            ),
            (
                """INSERT INTO portfolio_import_product_map
                    (active, source_id, company_id, external_sku, product_id)
                    SELECT active, source_id, company_id, external_sku, product_id
                    FROM portfolio_import_product_map WHERE id = %s""",
                [self.product_map.id],
            ),
        )
        for statement, params in statements:
            with self.subTest(statement=statement), self.assertRaises(IntegrityError), self.cr.savepoint():
                self.cr.execute(statement, params, log_exceptions=False)

    def test_source_identity_is_immutable_and_archive_is_supported(self):
        for field, value in (("company_id", self.company_b.id), ("company_code", "NEW"), ("code", "NEW")):
            with self.subTest(field=field), self.rejects((AccessError, UserError)):
                self.source_a.write({field: value})
        self.source_a.write({"active": False})
        self.assertFalse(self.source_a.active)

    def test_mapping_source_company_cannot_be_written_or_forged(self):
        for mapping, make in ((self.customer_map, self.customer_mapping), (self.product_map, self.product_mapping)):
            for values in ({"source_id": self.other_source.id}, {"company_id": self.company_b.id}):
                with self.subTest(model=mapping._name, values=values), self.rejects((AccessError, UserError)):
                    mapping.write(values)
            with self.rejects((AccessError, UserError)):
                make(self.other_source, company_id=self.company_b.id)
            mapping.write({"active": False})
            self.assertFalse(mapping.active)

    def test_public_copy_and_unlink_always_denied(self):
        for record in (self.source_a, self.customer_map, self.product_map):
            for method in ("copy", "unlink"):
                with self.subTest(model=record._name, method=method), self.rejects((AccessError, UserError)):
                    getattr(record, method)()

    def test_configuration_rejects_wrong_company_currency_pricelist_and_warehouse(self):
        usd_pricelist = self.native["product.pricelist"].create({
            "name": "Import USD", "currency_id": self.usd.id,
        })
        inactive_pricelist = self.shared_pricelist.copy({"active": False})
        for values in (
            {"pricelist_id": self.pricelist_b.id}, {"pricelist_id": usd_pricelist.id},
            {"pricelist_id": inactive_pricelist.id}, {"warehouse_id": self.warehouse_b.id},
            {"pricelist_id": False}, {"warehouse_id": False},
        ):
            with self.subTest(values=values), self.cr.savepoint(), self.rejects((ValidationError, UserError)):
                self.source(**values)
        for target, values in (
            (self.company_a, {"currency_id": self.usd.id}),
            (self.pricelist_a, {"active": False}),
            (self.warehouse_a, {"active": False}),
        ):
            with self.subTest(model=target._name), closing(self.cr.savepoint()):
                target.write(values)
                with self.rejects((ValidationError, UserError)):
                    self.source_a._validate_configuration()

    def test_salesperson_requires_active_internal_sales_and_source_company(self):
        internal = self.user("plain")
        portal = self.user("portal", "base.group_portal")
        inactive = self.user("inactive_sales", self.SALES, active=False)
        for user in (internal, portal, inactive, self.sales_b):
            with self.subTest(user=user.login), self.cr.savepoint(), self.rejects((ValidationError, UserError)):
                self.source(sales_user_id=user.id)

    def test_processor_requires_separate_active_single_company_operator_and_sales(self):
        candidates = (
            self.sales_a, self.user("only_operator", self.OPERATOR), self.integration_a,
            self.user("inactive_operator", f"{self.OPERATOR},{self.SALES}", active=False),
            self.operator_b,
            self.user("multicompany_operator", f"{self.OPERATOR},{self.SALES}", companies=self.companies),
        )
        for user in candidates:
            with self.subTest(user=user.login), self.cr.savepoint(), self.rejects((ValidationError, UserError)):
                self.source(processor_user_id=user.id)
        with self.rejects((ValidationError, UserError)):
            self.source(sales_user_id=self.operator_a.id)

    def test_processor_forbidden_privileges_are_rejected_without_implicit_changes(self):
        for index, group in enumerate(self.FORBIDDEN):
            user = self.user(f"privileged_processor_{index}", f"{self.OPERATOR},{self.SALES},{group}")
            before = (user.groups_id.ids, user.company_ids.ids)
            with self.subTest(group=group), self.cr.savepoint(), self.rejects((ValidationError, UserError)):
                self.source(processor_user_id=user.id)
            self.assertEqual((user.groups_id.ids, user.company_ids.ids), before)

    def test_integration_requires_active_internal_group_company_and_no_business_rights(self):
        candidates = [
            self.sales_a, self.user("plain_integration"), self.integration_b,
            self.user("inactive_integration", self.INTEGRATION, active=False),
            self.user("portal_integration", "base.group_portal"),
        ]
        candidates.extend(self.user(f"privileged_integration_{i}", f"{self.INTEGRATION},{group}")
                          for i, group in enumerate((*self.FORBIDDEN, self.SALES)))
        for user in candidates:
            before = (user.groups_id.ids, user.company_ids.ids)
            with self.subTest(user=user.login), self.cr.savepoint(), self.rejects((ValidationError, UserError)):
                self.source(integration_user_ids=[Command.set(user.ids)])
            self.assertEqual((user.groups_id.ids, user.company_ids.ids), before)

    def test_revalidation_detects_user_privilege_or_company_changes(self):
        for user, values in (
            (self.operator_a, {"active": False}),
            (self.integration_a, {"active": False}),
            (self.operator_a, {"groups_id": [Command.link(self.env.ref("stock.group_stock_user").id)]}),
            (self.integration_a, {"groups_id": [Command.link(self.env.ref(self.SALES).id)]}),
            (self.sales_a, {"company_id": self.company_b.id, "company_ids": [Command.set(self.company_b.ids)]}),
        ):
            with self.subTest(user=user.login, values=values), closing(self.cr.savepoint()):
                user.write(values)
                with self.rejects((ValidationError, UserError)):
                    self.source_a._validate_configuration()

    def test_ordinary_business_roles_have_no_configuration_read_or_mutation(self):
        roles = (self.SALES, "sales_team.group_sale_manager", "purchase.group_purchase_user",
                 "stock.group_stock_user", "account.group_account_invoice")
        for index, group in enumerate(roles):
            user = self.user(f"business_{index}", group)
            for record in (self.source_a, self.customer_map, self.product_map):
                model = self.actor(record._name, user)
                with self.subTest(group=group, model=record._name):
                    with self.assertRaises(AccessError):
                        model.search([])
                    with self.assertRaises(AccessError):
                        model.browse(record.id).read(["id"])
                    with self.assertRaises(AccessError):
                        model.browse(record.id).write({"active": False})

    def test_integration_reads_only_authorized_source_identifiers_and_no_maps(self):
        model = self.actor(self.SOURCE, self.integration_a)
        rows = model.search_read([], ["name", "company_id", "company_code", "code", "active"])
        self.assertEqual([row["id"] for row in rows], self.source_a.ids)
        self.assertEqual(model.search_count([]), 1)
        for record in (self.source_b, self.other_source):
            with self.assertRaises(AccessError):
                model.browse(record.id).read(["id"])
        for name in (self.CUSTOMER, self.PRODUCT):
            with self.assertRaises(AccessError):
                self.actor(name, self.integration_a).search([])
        self.assertFalse(self.integration_a.has_group(self.SALES))

    def test_operator_reads_only_assigned_sources_and_maps(self):
        for name, expected in ((self.SOURCE, self.source_a), (self.CUSTOMER, self.customer_map), (self.PRODUCT, self.product_map)):
            model = self.actor(name, self.operator_a)
            self.assertEqual(model.search([]).ids, expected.ids)
            self.assertEqual(model.search_count([]), 1)
        for record in (self.source_a, self.customer_map, self.product_map):
            with self.assertRaises(AccessError):
                record.with_user(self.operator_a).write({"active": False})
        with self.assertRaises(AccessError):
            self.actor(self.SOURCE, self.operator_a).create(self.source_values())

    def test_settings_are_limited_to_actual_and_active_company_scope(self):
        model = self.actor(self.SOURCE, self.settings)
        self.assertEqual(set(model.search([]).ids), {self.source_a.id, self.source_b.id, self.other_source.id})
        model_a = self.actor(self.SOURCE, self.settings, self.company_a)
        self.assertNotIn(self.source_b.id, model_a.search([]).ids)
        with self.assertRaises(AccessError):
            model_a.browse(self.source_b.id).read(["id"])
        with self.assertRaises(AccessError):
            model_a.browse(self.source_b.id).write({"name": "Forbidden"})
        with self.assertRaises(AccessError):
            self.actor(self.SOURCE, self.settings_a).create(self.source_values(self.company_b))

    def test_sudo_and_forged_company_context_do_not_expand_role_or_source_access(self):
        for user in (self.integration_a, self.operator_a, self.sales_a, self.settings_a):
            model = self.actor(self.SOURCE, user).sudo().with_context(
                allowed_company_ids=self.companies.ids, force_company=self.company_b.id,
                uid=self.settings.id, import_settings=True,
            )
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    model.browse(self.source_b.id).read(["id"])
                with self.assertRaises(AccessError):
                    model.browse(self.source_b.id).write({"name": "Forbidden"})
        with self.assertRaises(AccessError):
            self.actor(self.SOURCE, self.integration_a).sudo().create(self.source_values())

    def test_actual_settings_context_defaults_cannot_inject_foreign_company_or_source(self):
        model = self.actor(self.SOURCE, self.settings_a).with_context(default_company_id=self.company_b.id)
        values = self.source_values()
        values.pop("company_id")
        created = model.create(values)
        self.assertEqual(created.company_id, self.company_a)
        mapping = self.actor(self.CUSTOMER, self.settings_a).with_context(
            default_source_id=self.source_b.id, default_company_id=self.company_b.id,
        ).create({"source_id": self.other_source.id, "external_ref": "EXPLICIT", "partner_id": self.partner_a.id})
        self.assertEqual(mapping.company_id, self.company_a)
        with self.rejects((AccessError, UserError)):
            self.actor(self.CUSTOMER, self.settings_a).with_context(default_source_id=self.source_b.id).create({
                "external_ref": "NO-EXPLICIT-SOURCE", "partner_id": self.partner_a.id,
            })

    def test_stale_recordset_cannot_read_after_authorization_is_removed(self):
        record = self.source_a.with_user(self.integration_a).with_context(allowed_company_ids=self.company_a.ids)
        record.read(["name"])
        self.source_a.write({"integration_user_ids": [Command.clear()]})
        with self.assertRaises(AccessError):
            record.read(["name"])
        with self.assertRaises(AccessError):
            record.web_read({"id": {}})

    def test_stale_settings_recordset_cannot_read_after_company_membership_removed(self):
        record = self.source_b.with_user(self.settings).with_context(allowed_company_ids=self.companies.ids)
        record.read(["name"])
        self.settings.write({"company_ids": [Command.set(self.company_a.ids)]})
        with self.assertRaises(AccessError):
            record.read(["name"])
        with self.assertRaises(AccessError):
            record.web_read({"id": {}})

    def test_every_public_read_path_rechecks_actual_actor(self):
        for record in (self.other_source, self.customer_map_b, self.product_map_b):
            model = self.actor(record._name, self.operator_a).sudo()
            restricted = model.browse(record.id)
            for operation in (
                lambda: restricted.read(["id"]), lambda: restricted.web_read({"id": {}}),
                lambda: restricted.web_read({"name": {}}) if record._name == self.SOURCE else restricted.web_read({"active": {}}),
                lambda: restricted.export_data(["id"]),
            ):
                with self.subTest(model=record._name), self.assertRaises(AccessError):
                    operation()
            domain = [("id", "=", record.id)]
            self.assertFalse(model.search(domain))
            self.assertFalse(model.search_read(domain, ["id"]))
            self.assertEqual(model.search_count(domain), 0)
            self.assertFalse(model.read_group(domain, ["company_id"], ["company_id"]))

    def test_exists_is_internal_only_on_each_configuration_model(self):
        for record in (self.source_b, self.customer_map_b, self.product_map_b):
            # Exercise the dispatcher gate: api.call_kw alone does not reject private methods.
            for user in (self.integration_a, self.operator_a, self.sales_a, self.settings):
                with self.subTest(model=record._name, user=user.login), self.assertRaises(AccessError):
                    get_public_method(self.actor(record._name, user).sudo(), "exists")
            self.assertEqual(record.exists(), record)
        self.source_b._validate_configuration()
        self.customer_map_b._validate_target()
        self.product_map_b._validate_target()
        # The addon must not mutate the native helper's visibility on unrelated models.
        self.assertTrue(callable(get_public_method(self.native["res.partner"], "exists")))

    def test_integration_private_fields_are_denied_in_read_export_and_queries(self):
        model = self.actor(self.SOURCE, self.integration_a).sudo()
        record = model.browse(self.source_a.id)
        visible = model.fields_get()
        for field in self.PRIVATE_FIELDS:
            self.assertNotIn(field, visible)
            for operation_name, operation in (
                ("read", lambda field=field: record.read([field])),
                ("web_read", lambda field=field: record.web_read({field: {}})),
                ("export", lambda field=field: record.export_data([field])),
                ("search", lambda field=field: model.search([(field, "!=", False)])),
                ("order", lambda field=field: model.search([], order=field)),
                ("search_count", lambda field=field: model.search_count([(field, "!=", False)])),
                ("read_group", lambda field=field: model.read_group([], [field], [field])),
            ):
                with self.subTest(field=field, operation=operation_name), self.assertRaises(AccessError):
                    operation()
        for operation_name, operation in (
            ("m2m_dotted", lambda: model.search([("integration_user_ids.name", "ilike", "import")])),
            ("m2m_any", lambda: model.search([("integration_user_ids", "any", [("id", "=", self.integration_a.id)])])),
            ("m2m_group_aggregate", lambda: model.read_group([], ["people:count_distinct(integration_user_ids)"], [])),
        ):
            with self.subTest(operation=operation_name), self.assertRaises(AccessError):
                operation()

    def test_mapping_rejects_foreign_or_inactive_targets_and_allows_settings_repair(self):
        for mapping, field, wrong, shared in (
            (self.customer_map, "partner_id", self.partner_b, self.shared_partner),
            (self.product_map, "product_id", self.product_b, self.shared_product),
        ):
            with self.subTest(model=mapping._name), self.rejects((ValidationError, UserError)):
                mapping.write({field: wrong.id})
            with self.rejects((ValidationError, UserError)):
                mapping.write({field: False})
            mapping.write({field: shared.id})
            mapping._validate_target()
            self.assertEqual(mapping[field], shared)
            for values in ({"company_id": self.company_b.id}, {"active": False}):
                with closing(self.cr.savepoint()):
                    shared.write(values)
                    with self.rejects((ValidationError, UserError)):
                        mapping._validate_target()

    def test_create_mapping_rejects_cross_company_source_id_even_under_sudo(self):
        for name, key, target_field, target in (
            (self.CUSTOMER, "external_ref", "partner_id", self.partner_a),
            (self.PRODUCT, "external_sku", "product_id", self.product_a),
        ):
            model = self.actor(name, self.settings_a).sudo().with_context(
                allowed_company_ids=self.companies.ids,
            )
            with self.subTest(model=name), self.assertRaises(AccessError):
                model.create({"source_id": self.source_b.id, key: "CROSS-SOURCE", target_field: target.id})

    def test_mapping_revalidates_commercial_partner_company_and_active_state(self):
        parent = self.native["res.partner"].create({"name": "Import parent", "company_id": False})
        contact = self.native["res.partner"].create({"name": "Import contact", "parent_id": parent.id})
        self.customer_map.write({"partner_id": contact.id})
        for values in ({"company_id": self.company_b.id}, {"active": False}):
            with closing(self.cr.savepoint()):
                parent.write(values)
                with self.rejects((ValidationError, UserError)):
                    self.customer_map._validate_target()

    def test_batch_create_is_atomic_when_caller_catches_validation_failure(self):
        for name, values in (
            (self.SOURCE, [self.source_values(code="ATOMIC-GOOD"), self.source_values(code=" invalid")]),
            (self.CUSTOMER, [
                {"source_id": self.source_a.id, "external_ref": "ATOMIC-GOOD", "partner_id": self.partner_a.id},
                {"source_id": self.source_a.id, "external_ref": "ATOMIC-BAD", "partner_id": self.partner_b.id},
            ]),
            (self.PRODUCT, [
                {"source_id": self.source_a.id, "external_sku": "ATOMIC-GOOD", "product_id": self.product_a.id},
                {"source_id": self.source_a.id, "external_sku": "ATOMIC-BAD", "product_id": self.product_b.id},
            ]),
        ):
            model = self.actor(name).with_context(active_test=False)
            before = model.search([]).ids
            with self.subTest(model=name), self.cr.savepoint():
                with unittest.TestCase.assertRaises(self, (ValidationError, UserError, AccessError)):
                    model.create(values)
                self.env.flush_all()
                self.env.invalidate_all()
                self.assertEqual(model.search([]).ids, before)

    def test_mixed_company_batch_write_is_atomic_when_caller_catches_failure(self):
        for records in (self.source_a | self.source_b, self.customer_map | self.customer_map_b,
                        self.product_map | self.product_map_b):
            model = records.with_user(self.settings_a).with_context(allowed_company_ids=self.company_a.ids)
            before = records.read(["active"])
            with self.subTest(model=records._name), self.cr.savepoint():
                with unittest.TestCase.assertRaises(self, (AccessError, UserError)):
                    model.write({"active": False})
                self.env.flush_all()
                self.env.invalidate_all()
                self.assertEqual(records.read(["active"]), before)
