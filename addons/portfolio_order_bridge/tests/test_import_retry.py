"""Explicit operator retry and immutable source-scoped attempt history."""

import copy
import unittest
from contextlib import closing

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.service.model import get_public_method
from odoo.tests import tagged

from .common import ImportWorkerCase


@tagged("post_install", "-at_install", "portfolio_import_retry")
class TestImportRetry(ImportWorkerCase):
    def test_failed_repair_retry_then_success_retains_all_events(self):
        job = self.failed_job()
        original = job.read([
            "raw_payload", "canonical_payload", "payload_hash", "submitted_by_id", "received_at",
        ])
        failure = self.attempts(job)
        failure_values = failure.read()
        self.customer_map.write({"active": True})
        before = fields.Datetime.now()
        job.action_retry("  Mapping repaired by Settings  ")
        self.assertEqual((job.state, job.attempt_count), ("pending", 1))
        self.assertFalse(job.sale_order_id)
        events = self.attempts(job)
        self.assertEqual(events.mapped("event_type"), ["failed", "retry"])
        retry = events[-1]
        self.assertEqual(retry.attempt_no, 1)
        self.assertEqual(retry.retry_reason, "Mapping repaired by Settings")
        self.assertEqual(retry.actor_id, self.operator_a)
        self.assertGreaterEqual(retry.started_at, before)
        self.assertGreaterEqual(retry.finished_at, retry.started_at)
        self.assertEqual(failure.read(), failure_values)
        self.process()
        self.assertEqual((job.state, job.attempt_count), ("done", 2))
        self.assertTrue(job.sale_order_id)
        self.assertEqual(self.attempts(job).mapped("event_type"), ["failed", "retry", "done"])
        self.assertEqual(self.attempts(job).mapped("attempt_no"), [1, 1, 2])
        self.assertEqual(failure.read(), failure_values)
        self.assertEqual(job.read([
            "raw_payload", "canonical_payload", "payload_hash", "submitted_by_id", "received_at",
        ]), original)

    def test_retry_requires_nonempty_bounded_clean_reason_without_partial_writes(self):
        job = self.failed_job()
        for reason in (None, False, 1, "", " \n\t ", "x" * 513, "bad\x00reason", "bad\ud800reason"):
            with self.subTest(reason=repr(reason)), closing(self.cr.savepoint()):
                before = job.read()
                events = self.attempts(job).read()
                with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValidationError)):
                    job.action_retry(reason)
                self.env.flush_all()
                self.env.invalidate_all()
                self.assertEqual(job.read(), before)
                self.assertEqual(self.attempts(job).read(), events)

    def test_only_assigned_actual_operator_can_retry_even_under_retained_sudo(self):
        job = self.failed_job()
        original = job.read()
        events = self.attempts(job).ids
        for user in (self.integration_a, self.sales_a, self.settings, self.plain,
                     self.other_operator, self.operator_b):
            record = self.worker(user).sudo().with_context(
                import_retry=True, default_actor_id=self.operator_a.id,
                default_state="pending", default_company_id=self.company_a.id,
            ).browse(job.id)
            with self.subTest(user=user.login), unittest.TestCase.assertRaises(self, AccessError):
                record.action_retry("Forged retry")
            self.env.flush_all()
            self.env.invalidate_all()
            self.assertEqual(job.read(), original)
            self.assertEqual(self.attempts(job).ids, events)

    def test_retry_rejects_pending_done_and_multiple_jobs(self):
        pending = self.job(self.submit(), self.operator_a)
        with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValidationError)):
            pending.action_retry("Still pending")
        self.process()
        with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValidationError)):
            pending.action_retry("Already done")
        failed = self.failed_job()
        with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValidationError)):
            (pending | failed).action_retry("Multiple jobs")
        self.assertEqual((pending.state, failed.state), ("done", "failed"))

    def test_attempt_public_mutation_is_denied_for_all_actors_sudo_and_defaults(self):
        job = self.failed_job()
        event = self.attempts(job)
        original = event.read()
        forged = {
            "job_id": job.id, "company_id": self.company_b.id, "attempt_no": 999,
            "event_type": "done", "actor_id": self.settings.id,
            "started_at": fields.Datetime.now(), "finished_at": fields.Datetime.now(),
            "error_code": "FORGED", "error_path": "FORGED", "error_detail": "FORGED",
            "retry_reason": "FORGED", "mapping_snapshot": "{}", "source_defaults_snapshot": "{}",
        }
        for user in (self.integration_a, self.operator_a, self.sales_a, self.settings, self.plain):
            for sudo in (False, True):
                model = self.actor(self.ATTEMPT, user).sudo(sudo).with_context(
                    **{f"default_{key}": value for key, value in forged.items()},
                )
                record = model.browse(event.id)
                for operation in (
                    lambda: model.create(copy.deepcopy(forged)), lambda: model.create({}),
                    lambda: record.write(forged), lambda: record.write({}),
                    lambda: record.unlink(), lambda: record.copy(), lambda: record.copy_data(),
                ):
                    with self.subTest(user=user.login, sudo=sudo), self.assertRaises(AccessError):
                        operation()
        self.assertEqual(event.read(), original)
        for name in forged:
            self.assertTrue(self.actor(self.ATTEMPT, self.operator_a)._fields[name].readonly, name)

    def test_private_attempt_append_rejects_actor_source_and_state_forgery(self):
        job = self.failed_job()
        before = self.attempts(job).read()
        for user in (self.integration_a, self.sales_a, self.settings, self.plain,
                     self.other_operator, self.operator_b):
            model = self.actor(self.ATTEMPT, user).sudo().with_context(
                import_attempt=True, default_actor_id=self.operator_a.id,
                default_company_id=self.company_a.id, default_attempt_no=99,
            )
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                model._append_event(job.sudo(), "retry", {}, reason="forged")
        model = self.actor(self.ATTEMPT, self.operator_a)
        for kind in ("done", "failed", "unknown"):
            with self.subTest(kind=kind), unittest.TestCase.assertRaises(
                self, (AccessError, UserError, ValidationError),
            ):
                model._append_event(job.sudo(), kind, {})
        self.assertEqual(self.attempts(job).read(), before)

    def test_attempt_read_query_export_and_id_only_web_read_are_authorized(self):
        job = self.failed_job()
        event = self.attempts(job)
        for user in (self.integration_a, self.operator_a):
            model = self.actor(self.ATTEMPT, user).sudo()
            record = model.browse(event.id)
            self.assertEqual(model.search([]).ids, event.ids)
            self.assertEqual(model.search_count([]), 1)
            self.assertEqual(record.read(["id"]), [{"id": event.id}])
            self.assertEqual(record.web_read({"id": {}}), [{"id": event.id}])
            self.assertFalse(user.has_group("base.group_allow_export"))
            with self.assertRaisesRegex(UserError, "rights to export"):
                record.export_data(["id"])
            # Native export permission is a separate test-only grant. The
            # bridge never grants it or bypasses Odoo's global export gate.
            user.write({"groups_id": [Command.link(self.env.ref("base.group_allow_export").id)]})
            self.assertEqual(len(record.export_data(["id"])["datas"]), 1)
        for user in (self.sales_a, self.settings, self.plain):
            model = self.actor(self.ATTEMPT, user).sudo()
            for operation in (
                lambda: model.search([]), lambda: model.search_count([]),
                lambda: model.browse(event.id).read(["id"]),
                lambda: model.browse(event.id).web_read({"id": {}}),
                lambda: model.browse(event.id).export_data(["id"]),
            ):
                with self.subTest(user=user.login), self.assertRaises(AccessError):
                    operation()
        with self.assertRaises(AccessError):
            get_public_method(self.actor(self.ATTEMPT, self.operator_a), "exists")
        with self.assertRaises(AccessError):
            get_public_method(self.actor(self.ATTEMPT, self.operator_a), "_append_event")

    def test_attempt_reads_recheck_source_authorization_after_revocation(self):
        job = self.failed_job()
        event = self.attempts(job)
        integration_model = self.actor(self.ATTEMPT, self.integration_a).sudo()
        operator_model = self.actor(self.ATTEMPT, self.operator_a).sudo()
        self.assertTrue(integration_model.browse(event.id).read(["id"]))
        self.assertTrue(operator_model.browse(event.id).read(["id"]))
        self.source_a.write({
            "integration_user_ids": [Command.clear()], "processor_user_id": self.other_operator.id,
        })
        for model in (integration_model, operator_model):
            domain = [("id", "=", event.id)]
            for operation in (
                lambda: model.browse(event.id).read(["id"]),
                lambda: model.browse(event.id).web_read({"id": {}}),
                lambda: model.browse(event.id).export_data(["id"]),
            ):
                with self.assertRaises(AccessError):
                    operation()
            self.assertFalse(model.search(domain))
            self.assertFalse(model.search_fetch(domain, ["id"]))
            self.assertFalse(model.search_read(domain, ["id"]))
            self.assertEqual(model.search_count(domain), 0)
            self.assertFalse(model.read_group(domain, ["company_id"], ["company_id"]))
            self.assertFalse(model.name_search(args=domain))

    def test_attempt_company_and_other_source_are_hidden_under_sudo(self):
        job = self.failed_job()
        event = self.attempts(job)
        for user in (self.operator_b, self.integration_b, self.other_operator, self.other_integration):
            model = self.actor(self.ATTEMPT, user).sudo()
            self.assertFalse(model.search([]))
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                model.browse(event.id).read(["id"])
