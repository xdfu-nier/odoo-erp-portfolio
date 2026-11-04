"""Native ingress fixtures shared without inheriting any test methods."""

from odoo import Command
from odoo.tests import TransactionCase, new_test_user


class ImportIngressCase(TransactionCase):
    JOB = "portfolio.import.job"
    SOURCE = "portfolio.import.source"
    INTEGRATION = "portfolio_order_bridge.group_import_integration"
    OPERATOR = "portfolio_order_bridge.group_import_operator"
    SALES = "sales_team.group_sale_salesman"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cny = cls.env.ref("base.CNY")
        cls.usd = cls.env.ref("base.USD")
        cls.company_a = cls.env["res.company"].create({
            "name": "Ingress isolated company A", "currency_id": cls.cny.id,
        })
        cls.company_b = cls.env["res.company"].create({
            "name": "Ingress isolated company B", "currency_id": cls.cny.id,
        })
        cls.companies = cls.company_a | cls.company_b
        cls.native = cls.env(context={**cls.env.context, "allowed_company_ids": cls.companies.ids})
        for suffix, company in (("a", cls.company_a), ("b", cls.company_b)):
            warehouse = cls.native["stock.warehouse"].search([
                ("company_id", "=", company.id),
            ], limit=1)
            if not warehouse:
                warehouse = cls.native["stock.warehouse"].create({
                    "name": f"Ingress warehouse {suffix}", "code": f"IG{suffix.upper()}",
                    "company_id": company.id,
                })
            setattr(cls, f"warehouse_{suffix}", warehouse)
            setattr(cls, f"pricelist_{suffix}", cls.native["product.pricelist"].create({
                "name": f"Ingress CNY {suffix}", "currency_id": cls.cny.id,
                "company_id": company.id,
            }))
            for role, groups in (
                ("sales", cls.SALES), ("operator", f"{cls.OPERATOR},{cls.SALES}"),
                ("integration", cls.INTEGRATION),
            ):
                setattr(cls, f"{role}_{suffix}", cls.make_user(
                    f"{role}_{suffix}", groups, company,
                ))
        cls.settings = cls.make_user("settings", "base.group_system", cls.company_a,
                                     cls.companies)
        cls.other_operator = cls.make_user("other_operator", f"{cls.OPERATOR},{cls.SALES}")
        cls.other_integration = cls.make_user("other_integration", cls.INTEGRATION)
        cls.integration_both = cls.make_user("integration_both", cls.INTEGRATION,
                                             cls.company_a, cls.companies)
        cls.plain = cls.make_user("plain")

    @classmethod
    def make_user(cls, name, groups="base.group_user", company=None, companies=None, **values):
        company = company or cls.company_a
        return new_test_user(
            cls.env, login=f"ingress_{name}", groups=groups,
            company_id=company.id, company_ids=[Command.set((companies or company).ids)],
            context={"no_reset_password": True}, **values,
        )

    def actor(self, model=None, user=None, companies=None):
        user = user or self.integration_a
        return self.env[model or self.JOB].with_user(user).with_context(
            allowed_company_ids=(companies or user.company_ids).ids,
        )

    def source_values(self, company=None, **values):
        self.sequence += 1
        company = company or self.company_a
        suffix = "a" if company == self.company_a else "b"
        return {
            "name": f"Ingress source {self.sequence}", "company_id": company.id,
            "company_code": f"INGRESS-{suffix.upper()}", "code": f"SOURCE-{self.sequence}",
            "pricelist_id": getattr(self, f"pricelist_{suffix}").id,
            "warehouse_id": getattr(self, f"warehouse_{suffix}").id,
            "sales_user_id": getattr(self, f"sales_{suffix}").id,
            "processor_user_id": getattr(self, f"operator_{suffix}").id,
            "integration_user_ids": [Command.set((
                getattr(self, f"integration_{suffix}") | self.integration_both
            ).ids)],
            **values,
        }

    def source(self, company=None, **values):
        return self.actor(self.SOURCE, self.settings, self.companies).create(
            self.source_values(company, **values),
        )

    def payload(self, source=None, **values):
        source = source or self.source_a
        return {
            "company_code": source.company_code, "source_code": source.code,
            "external_order_id": "ORDER-1", "customer_ref": "UNMAPPED-CUSTOMER",
            "currency": "CNY", "request_id": "original-request", "sent_at": "2026-11-03T08:00:00Z",
            "lines": [
                {"external_line_id": "2", "sku": "UNMAPPED-SKU-2", "quantity": "1.5",
                 "unit_price": "12.50", "discount": "1.00"},
                {"external_line_id": "1", "sku": "UNMAPPED-SKU-1", "quantity": "2",
                 "unit_price": "20", "discount": "0"},
            ],
            **values,
        }

    def submit(self, payload=None, user=None, companies=None):
        return self.actor(user=user, companies=companies).submit_order(
            self.payload() if payload is None else payload,
        )

    def job(self, response, user=None):
        return self.actor(user=user).browse(response["job_id"])


class ImportWorkerCase(ImportIngressCase):
    """Worker fixtures without inheriting or duplicating any test methods."""

    ATTEMPT = "portfolio.import.attempt"

    def setUp(self):
        super().setUp()
        self.assertTrue(callable(getattr(self.env[self.JOB], "_process_pending", None)),
                        "M3 quotation worker entry is missing")
        self.assertIn(self.ATTEMPT, self.env.registry, "M3 immutable attempt model is missing")
        self.assertTrue(callable(getattr(self.env[self.JOB], "action_retry", None)),
                        "M3 manual retry entry is missing")
        self.sequence = 0
        self.source_a = self.source()
        self.source_b = self.source(self.company_b)
        self.other_source = self.source(
            processor_user_id=self.other_operator.id,
            integration_user_ids=[Command.set(self.other_integration.ids)],
        )
        self.company_a.with_user(self.settings).with_context(
            allowed_company_ids=self.companies.ids,
        ).write({
            "portfolio_masterdata_enabled": True, "portfolio_approval_enabled": True,
            "portfolio_approval_discount": 10, "portfolio_approval_amount": 10000,
        })
        self.customer = self.native["res.partner"].create({
            "name": "Worker native customer", "ref": "WORKER-CUSTOMER",
            "company_id": self.company_a.id,
        })
        self.vendor = self.native["res.partner"].create({
            "name": "Worker native supplier", "ref": "WORKER-VENDOR",
            "company_id": self.company_a.id,
        })
        category = self.native["uom.category"].create({"name": "Worker exact quantities"})
        self.unit = self.native["uom.uom"].create({
            "name": "Worker half-unit", "category_id": category.id,
            "uom_type": "reference", "rounding": 0.5,
        })
        self.products = self.native["product.product"]
        for index in (1, 2):
            product = self.native["product.product"].create({
                "name": f"Worker industrial item {index}", "default_code": f"WORKER-SKU-{index}",
                "company_id": self.company_a.id, "type": "consu", "is_storable": True,
                "invoice_policy": "delivery", "uom_id": self.unit.id, "uom_po_id": self.unit.id,
                "taxes_id": [Command.clear()], "supplier_taxes_id": [Command.clear()],
            })
            self.native["product.supplierinfo"].create({
                "product_tmpl_id": product.product_tmpl_id.id, "partner_id": self.vendor.id,
                "company_id": self.company_a.id, "currency_id": self.cny.id, "price": 5,
            })
            self.products |= product
        self.customer_map = self.actor("portfolio.import.customer.map", self.settings).create({
            "source_id": self.source_a.id, "external_ref": "UNMAPPED-CUSTOMER",
            "partner_id": self.customer.id,
        })
        self.product_maps = self.actor("portfolio.import.product.map", self.settings).create([
            {"source_id": self.source_a.id, "external_sku": f"UNMAPPED-SKU-{index}",
             "product_id": product.id}
            for index, product in enumerate(self.products, 1)
        ])

    def worker(self, user=None):
        return self.actor(user=user or self.operator_a)

    def process(self):
        self.worker()._process_pending()
        self.env.flush_all()
        self.env.invalidate_all()

    def attempts(self, job, user=None):
        return self.actor(self.ATTEMPT, user or self.operator_a).search(
            [("job_id", "=", job.id)], order="id",
        )

    def native_counts(self):
        return {
            model: self.native[model].search_count([("company_id", "=", self.company_a.id)])
            for model in ("sale.order", "sale.order.line", "stock.picking", "stock.move",
                          "purchase.order", "account.move")
        }

    def failed_job(self):
        self.customer_map.write({"active": False})
        self.sequence += 1
        job = self.job(self.submit(self.payload(external_order_id=f"FAILED-{self.sequence}")),
                       self.operator_a)
        self.process()
        self.assertEqual(job.state, "failed")
        return job
