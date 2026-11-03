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
