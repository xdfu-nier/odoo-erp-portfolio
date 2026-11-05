"""Related query domains honor every field's actual-actor read restrictions."""

import unittest

from odoo import api, models
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import ImportWorkerCase


@tagged("post_install", "-at_install", "portfolio_import_related_query")
class TestImportRelatedQuery(ImportWorkerCase):
    QUERY_METHODS = (
        "search", "search_fetch", "search_count", "search_read", "read_group", "name_search",
    )

    def setUp(self):
        super().setUp()
        self.query_job = self.job(self.submit(self.payload(
            external_order_id="RELATED-QUERY", customer_ref="RELATED-MISSING-CUSTOMER",
        )), self.operator_a)
        self.process()
        self.assertEqual(self.query_job.state, "failed")
        self.query_attempts = self.attempts(self.query_job)
        self.assertEqual(len(self.query_attempts), 1)

    def query_cases(self, user=None):
        return (
            (self.actor(self.JOB, user), self.query_job, ("source_id",)),
            (self.actor(self.ATTEMPT, user), self.query_attempts, ("job_id", "source_id")),
        )

    def related_domain(self, relations, leaf, syntax="dotted"):
        if syntax == "dotted":
            return [(".".join((*relations, leaf[0])), leaf[1], leaf[2])]
        domain = [leaf]
        for index, relation in reversed(list(enumerate(relations))):
            operator = "not any" if syntax == "not any" and index == 0 else "any"
            domain = [(relation, operator, domain)]
        return domain

    def query(self, model, method, domain, *, rpc=False, context=None):
        args, options = [domain], {}
        if method == "search_fetch":
            args.append(["id"])
        elif method == "search_read":
            options["fields"] = ["id"]
        elif method == "read_group":
            args.extend([["__count"], []])
        elif method == "name_search":
            args, options = [], {"args": domain}
        if method in {"search", "search_fetch", "search_read"}:
            options["order"] = "id"
        if rpc:
            return api.call_kw(model, method, args, {
                "context": context if context is not None else {
                    "allowed_company_ids": model.env.context["allowed_company_ids"],
                },
                **options,
            })
        return getattr(model, method)(*args, **options)

    def assert_query_result(self, model, method, domain, expected, *, rpc=False):
        result = self.query(model, method, domain, rpc=rpc)
        if method in {"search", "search_fetch"}:
            if rpc:
                self.assertIs(type(result), list)
                ids = result
            else:
                self.assertIsInstance(result, models.BaseModel)
                self.assertEqual(result._name, model._name)
                ids = result.ids
        elif method == "search_count":
            self.assertIs(type(result), int)
            self.assertEqual(result, len(expected))
            return
        elif method == "read_group":
            self.assertIs(type(result), list)
            self.assertEqual(sum(row["__count"] for row in result), len(expected))
            return
        elif method == "search_read":
            self.assertIs(type(result), list)
            ids = [row["id"] for row in result]
        else:
            self.assertIs(type(result), list)
            ids = [row[0] for row in result]
        self.assertTrue(all(type(record_id) is int and record_id > 0 for record_id in ids))
        self.assertEqual(sorted(ids), sorted(expected))

    def assert_private_queries_denied(self, cases, syntax="dotted"):
        for model, _records, relations in cases:
            for candidate in (self.integration_a, self.sales_a):
                domain = self.related_domain(
                    relations, ("integration_user_ids", "in", candidate.ids), syntax,
                )
                for method in self.QUERY_METHODS:
                    for rpc in (False, True):
                        with self.subTest(model=model._name, candidate=candidate.login,
                                          syntax=syntax, method=method, rpc=rpc):
                            with unittest.TestCase.assertRaises(self, AccessError):
                                self.query(model, method, domain, rpc=rpc)

    def test_direct_source_membership_reference_is_private_for_both_candidates(self):
        for sudo in (False, True):
            model = self.actor(self.SOURCE).sudo(sudo)
            for candidate in (self.integration_a, self.sales_a):
                with self.subTest(sudo=sudo, candidate=candidate.login):
                    with unittest.TestCase.assertRaises(self, AccessError):
                        self.query(model, "search_count", [
                            ("integration_user_ids", "in", candidate.ids),
                        ], rpc=True)

    def test_job_dotted_private_membership_rejected_by_public_queries(self):
        self.assert_private_queries_denied(self.query_cases()[:1])

    def test_attempt_dotted_private_membership_rejected_by_public_queries(self):
        self.assert_private_queries_denied(self.query_cases()[1:])

    def test_nested_any_private_membership_rejected_by_public_queries(self):
        self.assert_private_queries_denied(self.query_cases(), "any")

    def test_nested_not_any_private_membership_rejected_by_public_queries(self):
        self.assert_private_queries_denied(self.query_cases(), "not any")

    def test_private_many2one_and_mixed_relation_paths_are_denied(self):
        for model, _records, relations in self.query_cases():
            domains = (
                self.related_domain(relations, ("processor_user_id", "=", self.operator_a.id)),
                self.related_domain(relations, ("sales_user_id.name", "=", self.sales_a.name)),
                [(relations[0], "any", [
                    (".".join((*relations[1:], "integration_user_ids")), "any", [
                        ("id", "=", self.integration_a.id),
                    ]),
                ])],
                [(relations[0], "not any", [
                    (".".join((*relations[1:], "integration_user_ids")), "not any", [
                        ("id", "=", self.integration_a.id),
                    ]),
                ])],
            )
            for domain in domains:
                with self.subTest(model=model._name, domain=domain):
                    with unittest.TestCase.assertRaises(self, AccessError):
                        self.query(model, "search_count", domain, rpc=True)

    def test_retained_sudo_and_spoofed_actor_context_do_not_grant_field_access(self):
        context = {
            "allowed_company_ids": self.company_a.ids, "uid": self.settings.id,
            "import_settings": True, "import_ui": True, "force_company": self.company_b.id,
        }
        for model, _records, relations in self.query_cases():
            model = model.sudo().with_context(**context)
            self.assertEqual(model.env.uid, self.integration_a.id)
            for syntax in ("dotted", "any", "not any"):
                domain = self.related_domain(
                    relations, ("integration_user_ids", "in", self.integration_a.ids), syntax,
                )
                for rpc in (False, True):
                    with self.subTest(model=model._name, syntax=syntax, rpc=rpc):
                        with unittest.TestCase.assertRaises(self, AccessError):
                            self.query(model, "search_count", domain, rpc=rpc, context=context)

    def test_private_fields_are_checked_even_when_the_domain_has_no_matches(self):
        for model, _records, relations in self.query_cases():
            for syntax in ("dotted", "any", "not any"):
                domain = [("id", "=", 0), *self.related_domain(
                    relations, ("integration_user_ids", "in", self.integration_a.ids), syntax,
                )]
                with self.subTest(model=model._name, syntax=syntax):
                    with unittest.TestCase.assertRaises(self, AccessError):
                        self.query(model, "search_count", domain, rpc=True)

    def test_operator_private_related_queries_are_allowed_and_discriminate_candidates(self):
        for model, records, relations in self.query_cases(self.operator_a):
            for syntax in ("dotted", "any", "not any"):
                for candidate in (self.integration_a, self.sales_a):
                    matches = candidate == self.integration_a
                    if syntax == "not any":
                        matches = not matches
                    domain = [("id", "in", records.ids), *self.related_domain(
                        relations, ("integration_user_ids", "in", candidate.ids), syntax,
                    )]
                    for method in self.QUERY_METHODS:
                        for rpc in (False, True):
                            with self.subTest(model=model._name, candidate=candidate.login,
                                              syntax=syntax, method=method, rpc=rpc):
                                self.assert_query_result(
                                    model, method, domain, records.ids if matches else [], rpc=rpc,
                                )

    def test_settings_can_query_source_configuration_without_operational_model_acl(self):
        model = self.actor(self.SOURCE, self.settings, self.company_a)
        for candidate, expected in ((self.integration_a, self.source_a.ids), (self.sales_a, [])):
            domain = [("id", "=", self.source_a.id),
                      ("integration_user_ids", "in", candidate.ids)]
            self.assert_query_result(model, "search_count", domain, expected, rpc=True)
        for model, records, relations in self.query_cases(self.settings):
            domain = [("id", "in", records.ids), *self.related_domain(
                relations, ("integration_user_ids", "in", self.integration_a.ids),
            )]
            for method in self.QUERY_METHODS:
                with self.subTest(model=model._name, method=method):
                    with unittest.TestCase.assertRaises(self, AccessError):
                        self.query(model.sudo(), method, domain, rpc=True)

    def test_integration_public_related_queries_retain_native_orm_and_rpc_contracts(self):
        for model, records, relations in self.query_cases():
            leaves = (
                ("code", "=", self.source_a.code),
                ("name", "=", self.source_a.name),
                ("company_id.name", "=", self.company_a.name),
            )
            for syntax in ("dotted", "any", "not any"):
                for leaf in leaves:
                    domain = [("id", "in", records.ids), *self.related_domain(
                        relations, leaf, syntax,
                    )]
                    for method in self.QUERY_METHODS:
                        for rpc in (False, True):
                            with self.subTest(model=model._name, syntax=syntax, leaf=leaf[0],
                                              method=method, rpc=rpc):
                                self.assert_query_result(
                                    model, method, domain,
                                    [] if syntax == "not any" else records.ids, rpc=rpc,
                                )

    def test_public_related_domains_keep_source_company_and_actual_user_guards(self):
        other_response = self.submit(self.payload(
            self.other_source, external_order_id="RELATED-OTHER-SOURCE",
        ), user=self.other_integration)
        foreign_response = self.submit(self.payload(
            self.source_b, external_order_id="RELATED-OTHER-COMPANY",
        ), user=self.integration_b)
        self.worker(self.other_operator)._process_pending()
        self.worker(self.operator_b)._process_pending()
        self.env.flush_all()
        for model, own_records, relations in self.query_cases():
            if model._name == self.JOB:
                foreign_ids = [other_response["job_id"], foreign_response["job_id"]]
            else:
                foreign_ids = (
                    self.attempts(self.job(other_response, self.other_operator), self.other_operator)
                    | self.attempts(self.job(foreign_response, self.operator_b), self.operator_b)
                ).ids
            for user in (self.integration_a, self.other_integration, self.integration_b):
                scoped = self.actor(model._name, user).sudo()
                domain = [("id", "in", own_records.ids), *self.related_domain(
                    relations, ("company_id", "=", self.company_a.id), "any",
                )]
                self.assert_query_result(
                    scoped, "search", domain,
                    own_records.ids if user == self.integration_a else [], rpc=True,
                )
            self.assert_query_result(model.sudo(), "search", [
                ("id", "in", foreign_ids), *self.related_domain(
                    relations, ("name", "!=", False), "any",
                ),
            ], [], rpc=True)
            with unittest.TestCase.assertRaises(self, AccessError):
                self.query(model.sudo(), "search_count", [], rpc=True, context={
                    "allowed_company_ids": self.companies.ids, "uid": self.settings.id,
                })

    def test_invalid_related_domains_keep_native_value_errors(self):
        for model, _records, relations in self.query_cases():
            for domain in (
                ["|", ("id", "=", self.query_job.id)],
                self.related_domain(relations, ("missing_related_query_field", "=", 1)),
                self.related_domain(relations, ("missing_related_query_field", "=", 1), "any"),
            ):
                with self.subTest(model=model._name, domain=domain):
                    with unittest.TestCase.assertRaises(self, ValueError):
                        self.query(model, "search_count", domain, rpc=True)
