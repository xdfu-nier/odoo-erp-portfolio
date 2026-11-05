"""Native RPC search results retain Odoo's ID-list contract and actor boundaries."""

import unittest

from odoo import api, models
from odoo.exceptions import AccessError
from odoo.service.model import get_public_method
from odoo.tests import tagged

from .common import ImportWorkerCase


@tagged("post_install", "-at_install", "portfolio_import_rpc_contract")
class TestImportRpcContract(ImportWorkerCase):
    CUSTOMER = "portfolio.import.customer.map"
    PRODUCT = "portfolio.import.product.map"
    WIZARD = "portfolio.import.retry"

    def rpc(self, model, method, domain, context=None, **options):
        args = [domain, ["id"]] if method == "search_fetch" else [domain]
        return api.call_kw(model, method, args, {
            "context": context if context is not None else {
                "allowed_company_ids": model.env.context["allowed_company_ids"],
            },
            **options,
        })

    def assert_search_contract(self, model, method, domain, expected, **options):
        args = [domain, ["id"]] if method == "search_fetch" else [domain]
        records = getattr(model, method)(*args, **options)
        self.assertIsInstance(records, models.BaseModel)
        self.assertEqual(records._name, model._name)
        self.assertEqual(records.ids, expected)
        result = self.rpc(model, method, domain, **options)
        self.assertIs(type(result), list, f"{model._name}.{method} RPC must return an ID list")
        self.assertTrue(all(type(record_id) is int and record_id > 0 for record_id in result))
        self.assertEqual(result, expected)

    def configuration_records(self):
        return (
            (self.actor(self.SOURCE, self.settings, self.company_a),
             self.source_a | self.other_source),
            (self.actor(self.CUSTOMER, self.settings, self.company_a), self.customer_map),
            (self.actor(self.PRODUCT, self.settings, self.company_a), self.product_maps),
        )

    def operational_records(self):
        done = self.job(self.submit(self.payload(external_order_id="RPC-DONE")), self.operator_a)
        self.process()
        self.assertEqual(done.state, "done")
        failed = self.job(self.submit(self.payload(
            external_order_id="RPC-FAILED", customer_ref="RPC-MISSING-CUSTOMER",
        )), self.operator_a)
        self.process()
        self.assertEqual(failed.state, "failed")
        pending = self.job(self.submit(self.payload(external_order_id="RPC-PENDING")), self.operator_a)
        self.assertEqual(pending.state, "pending")
        attempts = self.attempts(done) | self.attempts(failed)
        self.assertEqual(attempts.mapped("event_type"), ["done", "failed"])
        wizard = self.actor(self.WIZARD, self.operator_a).create({
            "job_id": failed.id, "reason": "Repair the missing customer mapping",
        })
        return (
            (self.actor(self.JOB, self.integration_a), done | failed | pending),
            (self.actor(self.ATTEMPT, self.operator_a), attempts),
            (self.actor(self.WIZARD, self.operator_a), wizard),
        )

    def test_configuration_search_rpc_returns_ordered_positive_ids(self):
        for model, records in self.configuration_records():
            with self.subTest(model=model._name):
                self.assert_search_contract(
                    model, "search", [("id", "in", records.ids)],
                    sorted(records.ids, reverse=True), order="id desc",
                )
        model, sources = self.configuration_records()[0]
        self.assert_search_contract(
            model, "search", [("id", "in", sources.ids)],
            sorted(sources.ids, reverse=True)[1:2], order="id desc", offset=1, limit=1,
        )

    def test_search_fetch_rpc_returns_ids_for_each_import_model(self):
        for model, records in (*self.configuration_records(), *self.operational_records()):
            with self.subTest(model=model._name):
                self.assert_search_contract(
                    model, "search_fetch", [("id", "in", records.ids)],
                    sorted(records.ids, reverse=True), order="id desc",
                )

    def test_job_attempt_and_retry_search_rpc_returns_ids(self):
        for model, records in self.operational_records():
            with self.subTest(model=model._name):
                self.assert_search_contract(
                    model, "search", [("id", "in", records.ids)],
                    sorted(records.ids, reverse=True), order="id desc",
                )

    def test_empty_and_hidden_rpc_results_keep_role_source_and_company_guards(self):
        operational = self.operational_records()
        cases = (*self.configuration_records(), *operational)
        for method in ("search", "search_fetch"):
            for model, records in cases:
                for user in (self.sales_a, self.plain):
                    denied = self.actor(model._name, user).sudo()
                    self.assertEqual(denied.env.uid, user.id)
                    with self.subTest(method=method, model=model._name, user=user.login):
                        with unittest.TestCase.assertRaises(self, AccessError):
                            self.rpc(denied, method, [("id", "in", records.ids)])
                for user in (self.other_operator, self.operator_b):
                    hidden = self.actor(model._name, user).sudo()
                    with self.subTest(method=method, model=model._name, user=user.login):
                        self.assert_search_contract(
                            hidden, method, [("id", "=", records[0].id)], [],
                        )
                with self.subTest(method=method, model=model._name, scope="forged companies"):
                    with unittest.TestCase.assertRaises(self, AccessError):
                        self.rpc(
                            self.actor(model._name, self.operator_a).sudo(), method, [],
                            context={"allowed_company_ids": self.companies.ids,
                                     "uid": self.settings.id, "import_settings": True},
                        )
                with self.subTest(method=method, model=model._name, scope="authorized empty"):
                    self.assert_search_contract(model, method, [("id", "=", 0)], [])
            for name in (self.CUSTOMER, self.PRODUCT, self.WIZARD):
                with unittest.TestCase.assertRaises(self, AccessError):
                    self.rpc(self.actor(name, self.integration_a).sudo(), method, [])
            for name in (self.JOB, self.ATTEMPT, self.WIZARD):
                with unittest.TestCase.assertRaises(self, AccessError):
                    self.rpc(self.actor(name, self.settings).sudo(), method, [])
            integration = self.actor(self.SOURCE, self.integration_a).sudo()
            with unittest.TestCase.assertRaises(self, AccessError):
                args = [[], ["processor_user_id"]] if method == "search_fetch" else [
                    [("processor_user_id", "=", self.operator_a.id)],
                ]
                api.call_kw(integration, method, args, {
                    "context": {"allowed_company_ids": self.company_a.ids},
                })

    def test_private_dispatch_and_unrelated_native_search_contracts_are_retained(self):
        for model, records in (*self.configuration_records(), *self.operational_records()):
            with self.subTest(model=model._name):
                with unittest.TestCase.assertRaises(self, AccessError):
                    get_public_method(model, "exists")
                self.assertEqual(records.exists().ids, records.ids)
        partner = self.actor("res.partner", self.sales_a)
        self.assertTrue(callable(get_public_method(partner, "exists")))
        for method in ("search", "search_fetch"):
            self.assert_search_contract(
                partner, method, [("id", "=", self.customer.id)], self.customer.ids,
            )
        source = self.actor(self.SOURCE, self.settings, self.company_a)
        self.assert_search_contract(source, "search", [("id", "=", self.source_a.id)],
                                    self.source_a.ids)
