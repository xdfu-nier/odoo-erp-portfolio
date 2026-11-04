"""Native import console, current-actor retry forms and inactive source cron setup."""

import unittest

from lxml import etree
from odoo import SUPERUSER_ID, Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tools.safe_eval import safe_eval

from .common import ImportWorkerCase


@tagged("post_install", "-at_install", "portfolio_import_ui")
class TestImportUI(ImportWorkerCase):
    WIZARD = "portfolio.import.retry"
    VIEWS = {
        "source": "view_import_source_form",
        "customer": "view_import_customer_map_form",
        "product": "view_import_product_map_form",
        "job": "view_import_job_form",
        "attempt": "view_import_attempt_form",
        "retry": "view_import_retry_form",
    }
    ACTIONS = {
        "action_import_sources": "portfolio.import.source",
        "action_import_customer_maps": "portfolio.import.customer.map",
        "action_import_product_maps": "portfolio.import.product.map",
        "action_import_jobs": "portfolio.import.job",
        "action_import_attempts": "portfolio.import.attempt",
    }

    def setUp(self):
        # Missing features must report an assertion, before fixture creation or
        # env.ref can turn the first native RED into a registry/setup error.
        self.assertTrue(callable(getattr(self.env[self.JOB], "action_process_pending", None)),
                        "M3d native console processing action is missing")
        self.assertIn(self.WIZARD, self.env.registry, "M3d native retry wizard is missing")
        for name in ("action_open_retry", "action_open_sale_order"):
            self.assertTrue(callable(getattr(self.env[self.JOB], name, None)), name)
        for name in ("action_prepare_processing_cron", "action_open_customer_mappings",
                     "action_open_product_mappings"):
            self.assertTrue(callable(getattr(self.env[self.SOURCE], name, None)), name)
        for name in (*self.VIEWS.values(), *self.ACTIONS):
            self.assertTrue(self.env.ref(f"portfolio_order_bridge.{name}",
                                         raise_if_not_found=False), f"Missing native UI {name}")
        super().setUp()

    def ui_ref(self, name):
        return self.env.ref(f"portfolio_order_bridge.{name}")

    def ui_view(self, model, key, user, view_type="form"):
        result = self.actor(model, user).get_view(
            self.ui_ref(self.VIEWS[key]).id, view_type=view_type,
        )
        return etree.fromstring(result["arch"])

    def wizard(self, job, reason="Mapping repaired", user=None):
        return self.actor(self.WIZARD, user or self.operator_a).create({
            "job_id": job.id, "reason": reason,
        })

    def job_facts(self, job):
        self.env.flush_all()
        self.env.invalidate_all()
        return (job.read(), self.attempts(job).read(), self.native_counts())

    def assert_live_facts(self, job, expected):
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.job_facts(job), expected)

    def test_native_actions_and_views_resolve_for_ordinary_authorized_roles(self):
        for action_name, model in self.ACTIONS.items():
            user = self.settings if model in (
                self.SOURCE, "portfolio.import.customer.map", "portfolio.import.product.map",
            ) else self.operator_a
            action = self.actor("ir.actions.actions", user)._for_xml_id(
                f"portfolio_order_bridge.{action_name}",
            )
            self.assertEqual(action["res_model"], model)
            self.assertEqual(action["type"], "ir.actions.act_window")
            self.assertIn("list", action["view_mode"].split(","))
            self.assertIn("form", action["view_mode"].split(","))
            views = self.actor(model, user).get_views([(False, "list"), (False, "form")])
            for kind in ("list", "form"):
                arch = etree.fromstring(views["views"][kind]["arch"])
                self.assertEqual(arch.tag, kind)
                self.assertFalse(arch.xpath(".//*[@attrs or @states]"))
        for user in (self.integration_a, self.operator_a):
            for model, key in ((self.JOB, "job"), (self.ATTEMPT, "attempt")):
                self.assertEqual(self.ui_view(model, key, user).tag, "form")
            source_arch = self.ui_view(self.SOURCE, "source", user)
            self.assertEqual(source_arch.tag, "form")
            self.assertFalse(source_arch.xpath(".//field[@name='processing_cron_id']"))
            if user == self.integration_a:
                for field in ("pricelist_id", "warehouse_id", "processor_user_id", "integration_user_ids"):
                    self.assertFalse(source_arch.xpath(f".//field[@name='{field}']"))

    def test_menus_are_under_portfolio_and_exclude_sales_and_unrelated_users(self):
        parent = self.env.ref("portfolio_trade.menu_portfolio_root")
        menus = self.env["ir.ui.menu"].sudo().with_context(**{"ir.ui.menu.full_list": True}).search([
            ("id", "child_of", parent.id),
            ("action", "in", [f"ir.actions.act_window,{self.ui_ref(name).id}"
                              for name in self.ACTIONS]),
        ])
        self.assertEqual(len(menus), len(self.ACTIONS))
        for user in (self.sales_a, self.plain):
            visible = self.actor("ir.ui.menu", user)._visible_menu_ids()
            self.assertFalse(set(menus.ids).intersection(visible))
            for model in (self.SOURCE, self.JOB, self.ATTEMPT, self.WIZARD):
                self.assertFalse(self.actor(model, user).has_access("read"))
        for user in (self.integration_a, self.operator_a):
            visible = self.actor("ir.ui.menu", user)._visible_menu_ids()
            job_action = self.ui_ref("action_import_jobs")
            self.assertTrue(menus.filtered(
                lambda menu: menu.action == job_action and menu.id in visible,
            ))
            config_actions = {self.ui_ref(name).id for name in (
                "action_import_sources", "action_import_customer_maps", "action_import_product_maps",
            )}
            self.assertFalse(menus.filtered(
                lambda menu: menu.action.id in config_actions and menu.id in visible,
            ))

    def test_new_source_defaults_actual_company_and_identity_is_editable_until_save(self):
        model = self.actor(self.SOURCE, self.settings, self.companies)
        self.assertEqual(model.default_get(["company_id"])["company_id"], self.company_a.id)
        with Form(model, view=self.ui_ref(self.VIEWS["source"])) as source_form:
            self.assertEqual(source_form.company_id, self.company_a)
            source_form.name = "Native source creation"
            source_form.company_id = self.company_a
            source_form.company_code = "NATIVE-COMPANY"
            source_form.code = "NATIVE-SOURCE"
            source_form.pricelist_id = self.pricelist_a
            source_form.warehouse_id = self.warehouse_a
            source_form.sales_user_id = self.sales_a
            source_form.processor_user_id = self.operator_a
            source_form.integration_user_ids.add(self.integration_a)
        source = source_form.record
        self.assertEqual((source.company_code, source.code), ("NATIVE-COMPANY", "NATIVE-SOURCE"))
        for name, value in (("company_id", self.company_b), ("company_code", "CHANGED"),
                            ("code", "CHANGED")):
            with self.subTest(field=name), self.assertRaises(AssertionError):
                setattr(Form(source, view=self.ui_ref(self.VIEWS["source"])), name, value)
            with unittest.TestCase.assertRaises(self, (UserError, AccessError)):
                source.write({name: value.id if name == "company_id" else value})

    def test_mapping_forms_prefill_source_and_allow_native_target_repairs_and_archival(self):
        replacement_customer = self.customer.copy({"name": "Repaired native customer"})
        for model, key, external, target, replacement in (
            ("portfolio.import.customer.map", "customer", "external_ref", "partner_id",
             replacement_customer),
            ("portfolio.import.product.map", "product", "external_sku", "product_id",
             self.products[-1]),
        ):
            native = self.actor(model, self.settings).with_context(default_source_id=self.source_a.id)
            with Form(native, view=self.ui_ref(self.VIEWS[key])) as mapping_form:
                self.assertEqual(mapping_form.source_id, self.source_a)
                mapping_form.source_id = self.source_a
                setattr(mapping_form, external, f"NATIVE-{key.upper()}")
                setattr(mapping_form, target, replacement)
            mapping = mapping_form.record
            self.assertEqual(mapping.source_id, self.source_a)
            with self.assertRaises(AssertionError):
                Form(mapping, view=self.ui_ref(self.VIEWS[key])).source_id = self.other_source
            with Form(mapping, view=self.ui_ref(self.VIEWS[key])) as repair_form:
                setattr(repair_form, target, replacement)
                repair_form.active = False
            self.assertFalse(mapping.active)
            self.assertEqual(mapping[target], replacement)

    def test_job_and_attempt_views_are_readonly_and_audit_tabs_keep_field_security(self):
        for user in (self.integration_a, self.operator_a):
            for model, key in ((self.JOB, "job"), (self.ATTEMPT, "attempt")):
                for kind in ("list", "form"):
                    view = self.actor(model, user).get_view(view_type=kind)
                    arch = etree.fromstring(view["arch"])
                    for attribute in ("create", "edit", "delete"):
                        self.assertEqual(arch.get(attribute), "false", (model, kind, attribute))
                self.assertTrue(self.ui_view(model, key, user).xpath(".//notebook/page"))
            job_arch = self.ui_view(self.JOB, "job", user)
            self.assertTrue(job_arch.xpath(".//field[@name='attempt_ids']"))
            for name in ("raw_payload", "canonical_payload", "payload_hash"):
                self.assertTrue(job_arch.xpath(f".//notebook/page//field[@name='{name}']"))
                self.assertTrue(self.actor(self.JOB, user)._fields[name].readonly)
        for user in (self.sales_a, self.plain, self.settings):
            meta = self.actor(self.JOB, user).sudo().fields_get()
            self.assertNotIn("raw_payload", meta)
            self.assertNotIn("attempt_ids", meta)

    def test_form_buttons_follow_actual_roles_and_current_job_state(self):
        operator = self.ui_view(self.JOB, "job", self.operator_a)
        integration = self.ui_view(self.JOB, "job", self.integration_a)
        for name in ("action_process_pending", "action_open_retry", "action_open_sale_order"):
            self.assertTrue(operator.xpath(f".//button[@name='{name}']"), name)
            self.assertFalse(integration.xpath(f".//button[@name='{name}']"), name)
        retry_button = operator.xpath(".//button[@name='action_open_retry']")[0]
        sale_button = operator.xpath(".//button[@name='action_open_sale_order']")[0]
        for state in ("pending", "processing", "failed", "done"):
            context = {"state": state, "sale_order_id": state == "done", "id": 1}
            self.assertEqual(bool(safe_eval(retry_button.get("invisible", "False"), context)),
                             state != "failed")
            self.assertEqual(bool(safe_eval(sale_button.get("invisible", "False"), context)),
                             state != "done")

    def test_manual_process_action_creates_one_native_quotation_as_actual_operator(self):
        job = self.job(self.submit(), self.operator_a)
        before = self.native_counts()
        result = job.action_process_pending()
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.sale_order_id.state, "draft")
        self.assertEqual(job.sale_order_id.create_uid, self.operator_a)
        self.assertEqual(self.native_counts()["sale.order"], before["sale.order"] + 1)
        self.assertEqual(result["type"], "ir.actions.client")
        self.assertEqual(result["tag"], "display_notification")
        self.assertIn("1", result["params"]["message"])
        self.assertEqual(self.attempts(job).actor_id, self.operator_a)

    def test_model_process_action_uses_bounded_five_job_queue(self):
        jobs = self.worker().browse()
        for index in range(6):
            jobs |= self.job(self.submit(self.payload(external_order_id=f"QUEUE-{index}")),
                             self.operator_a)
        self.worker().action_process_pending()
        self.env.invalidate_all()
        self.assertEqual(len(jobs.filtered(lambda job: job.state == "done")), 5)
        self.assertEqual(len(jobs.filtered(lambda job: job.state == "pending")), 1)
        self.assertEqual(jobs.filtered(lambda job: job.state == "done").mapped("attempt_count"),
                         [1] * 5)

    def test_process_action_denies_wrong_actual_roles_and_foreign_selected_jobs(self):
        job = self.job(self.submit(), self.operator_a)
        before = self.job_facts(job)
        for user in (self.integration_a, self.sales_a, self.settings, self.plain,
                     self.other_operator, self.operator_b):
            forged = self.worker(user).sudo().with_context(
                import_ui=True, default_processor_user_id=self.operator_a.id,
            ).browse(job.id)
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                forged.action_process_pending()
            self.assert_live_facts(job, before)
        other = self.job(self.submit(self.payload(self.other_source), self.other_integration),
                         self.other_operator)
        with self.assertRaises(AccessError):
            (job | self.worker().browse(other.id)).action_process_pending()
        self.assert_live_facts(job, before)
        self.assertEqual(other.state, "pending")

    def test_source_mapping_actions_are_exact_scoped_and_reauthorize_actual_actor(self):
        for method, model in (("action_open_customer_mappings", "portfolio.import.customer.map"),
                              ("action_open_product_mappings", "portfolio.import.product.map")):
            for user in (self.settings, self.operator_a):
                action = getattr(self.actor(self.SOURCE, user).browse(self.source_a.id), method)()
                self.assertEqual(action["res_model"], model)
                domain = action["domain"]
                if isinstance(domain, str):
                    domain = safe_eval(domain)
                self.assertEqual(domain, [("source_id", "=", self.source_a.id)])
                context = action.get("context", {})
                if isinstance(context, str):
                    context = safe_eval(context)
                self.assertEqual(context["default_source_id"], self.source_a.id)
            for user in (self.integration_a, self.sales_a, self.plain,
                         self.other_operator, self.operator_b):
                record = self.actor(self.SOURCE, user).sudo().with_context(
                    default_source_id=self.source_a.id, import_settings=True,
                ).browse(self.source_a.id)
                with self.subTest(method=method, user=user.login), self.assertRaises(AccessError):
                    getattr(record, method)()
            with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValueError)):
                getattr(self.source_a | self.other_source, method)()

    def test_mapping_action_rechecks_source_and_company_after_revocation(self):
        retained = self.actor(self.SOURCE, self.operator_a).sudo().browse(self.source_a.id)
        self.source_a.write({"processor_user_id": self.other_operator.id})
        for method in ("action_open_customer_mappings", "action_open_product_mappings"):
            with self.assertRaises(AccessError):
                getattr(retained, method)()
            with self.assertRaises(AccessError):
                getattr(self.actor(self.SOURCE, self.settings, self.company_b).browse(
                    self.source_a.id,
                ), method)()

    def test_open_native_sale_order_checks_job_and_native_sale_access(self):
        job = self.job(self.submit(), self.operator_a)
        with unittest.TestCase.assertRaises(self, (UserError, AccessError)):
            job.action_open_sale_order()
        job.action_process_pending()
        action = job.action_open_sale_order()
        self.assertEqual((action["res_model"], action["res_id"], action["view_mode"]),
                         ("sale.order", job.sale_order_id.id, "form"))
        job.sale_order_id.check_access("read")
        for user in (self.integration_a, self.sales_a, self.other_operator, self.settings):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.actor(self.JOB, user).sudo().browse(job.id).action_open_sale_order()

    def test_native_retry_form_saves_readonly_default_job_and_applies_trimmed_reason(self):
        job = self.failed_job()
        action = job.action_open_retry()
        self.assertEqual((action["res_model"], action["target"]), (self.WIZARD, "new"))
        context = action["context"]
        self.assertEqual(context["default_job_id"], job.id)
        original = job.read(["raw_payload", "canonical_payload", "payload_hash"])
        self.customer_map.write({"active": True})
        model = self.actor(self.WIZARD, self.operator_a).with_context(**context)
        with Form(model, view=self.ui_ref(self.VIEWS["retry"])) as retry_form:
            self.assertEqual(retry_form.job_id, job)
            retry_form.reason = "  Customer mapping repaired  "
        wizard = retry_form.record
        self.assertEqual(wizard.job_id, job)
        self.assertEqual(wizard.company_id, self.company_a)
        self.assertEqual(wizard.reason, "Customer mapping repaired")
        self.assertTrue(model._fields["job_id"].readonly)
        self.assertTrue(model._fields["company_id"].store)
        self.assertEqual(wizard.action_apply()["type"], "ir.actions.act_window_close")
        self.assertEqual(job.state, "pending")
        retry = self.attempts(job)[-1]
        self.assertEqual((retry.event_type, retry.retry_reason, retry.actor_id),
                         ("retry", "Customer mapping repaired", self.operator_a))
        job.action_process_pending()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.read(["raw_payload", "canonical_payload", "payload_hash"]), original)
        self.assertEqual(self.attempts(job).mapped("event_type"), ["failed", "retry", "done"])

    def test_retry_open_requires_exact_current_failed_assigned_operator_job(self):
        failed = self.failed_job()
        pending = self.job(self.submit(self.payload(external_order_id="RETRY-PENDING")),
                           self.operator_a)
        self.assertEqual(pending.state, "pending")
        for user in (self.integration_a, self.sales_a, self.settings, self.plain,
                     self.other_operator, self.operator_b):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.actor(self.JOB, user).sudo().browse(failed.id).action_open_retry()
        for records in (self.worker(), pending, pending | failed):
            with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValueError)):
                records.action_open_retry()
        self.assertEqual(failed.state, "failed")
        self.assertEqual(self.attempts(failed).mapped("event_type"), ["failed"])

    def test_retry_wizard_requires_explicit_job_and_rejects_rebinding_and_derived_company(self):
        job = self.failed_job()
        model = self.actor(self.WIZARD, self.operator_a).with_context(default_job_id=job.id)
        before = self.job_facts(job)
        for values in ({"reason": "Implicit job"}, {"job_id": job.id, "reason": "Company",
                       "company_id": self.company_b.id}):
            with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValidationError)):
                model.create(values)
            self.assert_live_facts(job, before)
        wizard = self.wizard(job)
        for values in ({"job_id": job.id}, {"company_id": self.company_b.id}):
            with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValidationError)):
                wizard.write(values)
            self.assert_live_facts(job, before)
        self.assertEqual(wizard.job_id, job)

    def test_retry_wizard_validates_clean_bounded_reason_before_any_mutation(self):
        job = self.failed_job()
        wizard = self.wizard(job)
        before = self.job_facts(job)
        for reason in (False, 1, "", " \n\t ", "x" * 513, "bad\x00reason", "bad\ud800reason"):
            for operation in (lambda reason=reason: self.wizard(job, reason),
                              lambda reason=reason: wizard.write({"reason": reason})):
                with self.subTest(reason=repr(reason)), unittest.TestCase.assertRaises(
                    self, (AccessError, UserError, ValidationError),
                ):
                    operation()
                self.assert_live_facts(job, before)
                self.assertEqual(wizard.reason, "Mapping repaired")
        wizard.write({"reason": "  Repaired again  "})
        self.assertEqual(wizard.reason, "Repaired again")

    def test_retry_wizard_actual_role_creator_and_source_ownership_survive_sudo(self):
        job = self.failed_job()
        wizard = self.wizard(job)
        before = self.job_facts(job)
        for user in (self.integration_a, self.sales_a, self.settings, self.plain,
                     self.other_operator, self.operator_b):
            model = self.actor(self.WIZARD, user).sudo().with_context(
                default_job_id=job.id, default_create_uid=self.operator_a.id,
                import_retry=True, default_company_id=self.company_a.id,
            )
            record = model.browse(wizard.id)
            operations = (
                lambda: model.create({"job_id": job.id, "reason": "Forged"}),
                lambda: record.read(["id"]), lambda: record.web_read({"id": {}}),
                lambda: record.export_data(["id"]),
                lambda: record.write({"reason": "Forged"}), lambda: record.action_apply(),
                lambda: record.unlink(),
            )
            for operation in operations:
                with self.subTest(user=user.login), self.assertRaises(AccessError):
                    operation()
                self.assert_live_facts(job, before)
        # A second otherwise valid operator assigned to this same source still
        # cannot inherit the first user's transient merely by retaining sudo.
        self.source_a.write({"processor_user_id": self.other_operator.id})
        with self.assertRaises(AccessError):
            self.actor(self.WIZARD, self.other_operator).sudo().browse(wizard.id).read(["id"])

    def test_retry_wizard_rejects_nonfailed_stale_and_foreign_environment_jobs(self):
        pending = self.job(self.submit(), self.operator_a)
        with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValidationError)):
            self.wizard(pending)
        job = self.failed_job()
        wizard = self.wizard(job)
        # A foreign recordset must be reduced to its ID and checked in the
        # caller's real environment, never trusted as an authorization token.
        with self.assertRaises(AccessError):
            self.actor(self.WIZARD, self.other_operator).sudo().create({
                "job_id": job.sudo(), "reason": "Foreign environment",
            })
        job.action_retry("Independent operator action")
        before = self.job_facts(job)
        for operation in (lambda: wizard.read(["id"]), lambda: wizard.web_read({"id": {}}),
                          lambda: wizard.export_data(["id"]),
                          lambda: wizard.write({"reason": "Stale"}), lambda: wizard.action_apply()):
            with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValidationError)):
                operation()
            self.assert_live_facts(job, before)

    def test_retry_wizard_rechecks_current_source_group_and_company_before_id_only_reads(self):
        job = self.failed_job()
        wizard = self.wizard(job)
        self.source_a.write({"processor_user_id": self.other_operator.id})
        for operation in (lambda: wizard.sudo().read(["id"]),
                          lambda: wizard.sudo().web_read({"id": {}}),
                          lambda: wizard.sudo().export_data(["id"]),
                          lambda: wizard.sudo().action_apply()):
            with self.assertRaises(AccessError):
                operation()
        self.assertEqual(self.attempts(job, self.other_operator).mapped("event_type"), ["failed"])
        self.source_a.write({"processor_user_id": self.operator_a.id})
        with self.assertRaises(AccessError):
            wizard.sudo().with_context(allowed_company_ids=self.company_b.ids).read(["id"])
        group = self.env.ref(self.OPERATOR)
        try:
            self.operator_a.write({"groups_id": [Command.unlink(group.id)]})
            with self.assertRaises(AccessError):
                wizard.sudo().web_read({"id": {}})
        finally:
            self.operator_a.write({"groups_id": [Command.link(group.id)]})
            self.env.registry.clear_cache()

    def test_retry_wizard_queries_reject_owner_who_lost_processing_eligibility(self):
        job = self.failed_job()
        wizard = self.wizard(job, "Private repair reason")
        before = self.job_facts(job)
        model = self.actor(self.WIZARD, self.operator_a)
        self.assertEqual(model.search([("id", "=", wizard.id)]), wizard)
        group = self.env.ref("sales_team.group_sale_salesman_all_leads")
        try:
            self.operator_a.write({"groups_id": [Command.link(group.id)]})
            with self.assertRaises(AccessError):
                wizard.read(["id", "reason"])
            for sudo in (False, True):
                queried = model.sudo(sudo)
                for operation in (
                    lambda: queried.search([("id", "=", wizard.id)]),
                    lambda: queried.search_count([("id", "=", wizard.id)]),
                    lambda: queried.read_group([("id", "=", wizard.id)], ["reason"], ["reason"]),
                ):
                    with self.subTest(sudo=sudo), self.assertRaises(AccessError):
                        operation()
                    self.assert_live_facts(job, before)
        finally:
            self.operator_a.write({"groups_id": [Command.unlink(group.id)]})
            self.env.registry.clear_cache()

    def test_transient_cleanup_remains_available_only_to_true_superuser(self):
        job = self.failed_job()
        wizard = self.wizard(job)
        wizard_id = wizard.id
        before = self.job_facts(job)
        wizard.flush_recordset()
        # Fixture aging exercises the actual native scheduled cleanup path;
        # business write() cannot forge the technical expiration timestamps.
        self.cr.execute("""
            UPDATE portfolio_import_retry
               SET create_date = (now() AT TIME ZONE 'UTC') - interval '2 hours',
                   write_date = (now() AT TIME ZONE 'UTC') - interval '2 hours'
             WHERE id = %s
        """, [wizard_id])
        wizard.invalidate_recordset()
        self.actor(self.WIZARD, self.operator_a).with_user(SUPERUSER_ID)._transient_vacuum()
        self.assertFalse(self.env[self.WIZARD].with_user(SUPERUSER_ID).browse(wizard_id).exists())
        self.assert_live_facts(job, before)

    def test_attempt_relation_is_readonly_and_cannot_mutate_audit_through_commands(self):
        job = self.failed_job()
        events = self.attempts(job)
        self.assertEqual(job.attempt_ids, events)
        self.assertEqual(self.actor(self.JOB, self.integration_a).browse(job.id).attempt_ids, events)
        field = job._fields["attempt_ids"]
        self.assertTrue(field.readonly)
        self.assertEqual(field.comodel_name, self.ATTEMPT)
        self.assertEqual(field.inverse_name, "job_id")
        before = self.job_facts(job)
        for command in (Command.clear(), Command.delete(events.id),
                        Command.update(events.id, {"retry_reason": "Forged"}),
                        Command.create({"event_type": "retry"})):
            with self.assertRaises(AccessError):
                job.sudo().write({"attempt_ids": [command]})
            self.assert_live_facts(job, before)

    def test_prepare_cron_creates_one_inactive_fixed_native_action_and_reuses_exact_link(self):
        source = self.source_a
        self.assertFalse(source.processing_cron_id)
        source.action_prepare_processing_cron()
        self.env.flush_all()
        self.env.invalidate_all()
        cron = source.processing_cron_id
        self.assertTrue(cron)
        self.assertFalse(cron.active)
        self.assertEqual(cron.user_id, self.operator_a)
        self.assertEqual(cron.model_id.model, self.JOB)
        self.assertEqual(cron.code, "model._process_pending()")
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "minutes"))
        source.action_prepare_processing_cron()
        self.assertEqual(source.processing_cron_id, cron)
        self.assertFalse(cron.active)
        cron.with_user(self.settings).write({"active": True})
        source.action_prepare_processing_cron()
        self.assertEqual(source.processing_cron_id, cron)
        self.assertTrue(cron.active)

    def test_processing_cron_link_is_settings_only_and_publicly_server_controlled(self):
        source = self.source_a
        source.action_prepare_processing_cron()
        cron = source.processing_cron_id
        self.assertTrue(source._fields["processing_cron_id"].readonly)
        for user in (self.integration_a, self.operator_a, self.sales_a, self.plain):
            self.assertNotIn("processing_cron_id", self.actor(self.SOURCE, user).sudo().fields_get())
            with self.assertRaises(AccessError):
                self.actor(self.SOURCE, user).sudo().browse(source.id).read(["processing_cron_id"])
        for value in (False, cron.id):
            with self.assertRaises(AccessError):
                source.write({"processing_cron_id": value})
            with self.assertRaises(AccessError):
                self.actor(self.SOURCE, self.settings).create(self.source_values(
                    processing_cron_id=value,
                ))
        self.assertEqual(source.processing_cron_id, cron)

    def test_context_and_model_defaults_cannot_inject_processing_cron_link(self):
        self.source_a.action_prepare_processing_cron()
        cron = self.source_a.processing_cron_id
        defaults = self.env["ir.default"].sudo()
        defaults.set(self.SOURCE, "processing_cron_id", cron.id, user_id=self.settings.id)
        try:
            source = self.actor(self.SOURCE, self.settings).with_context(
                default_processing_cron_id=cron.id, default_ingress_revision=99,
            ).create(self.source_values())
            self.assertFalse(source.processing_cron_id)
            self.assertEqual(source.ingress_revision, 0)
            self.assertFalse(source.with_user(self.settings).default_get(
                ["processing_cron_id"],
            ).get("processing_cron_id"))
        finally:
            defaults.discard_values(self.SOURCE, "processing_cron_id", [cron.id])

    def test_cron_preparation_rejects_actual_wrong_roles_company_and_multiple_sources(self):
        before = self.env["ir.cron"].sudo().search_count([])
        for user in (self.integration_a, self.operator_a, self.sales_a, self.plain,
                     self.other_operator, self.operator_b):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.actor(self.SOURCE, user).sudo().with_context(
                    default_user_id=self.settings.id, import_settings=True,
                ).browse(self.source_a.id).action_prepare_processing_cron()
        for records in (self.actor(self.SOURCE, self.settings), self.source_a | self.other_source):
            with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValueError)):
                records.action_prepare_processing_cron()
        with self.assertRaises(AccessError):
            self.actor(self.SOURCE, self.settings, self.company_b).sudo().browse(
                self.source_a.id,
            ).action_prepare_processing_cron()
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertFalse(self.source_a.processing_cron_id)
        self.assertEqual(self.env["ir.cron"].sudo().search_count([]), before)
        self.assertFalse(self.actor("ir.cron", self.operator_a).has_access("write"))
        self.assertFalse(self.operator_a.has_group("base.group_system"))

    def test_cron_preparation_revalidates_processor_without_enabling_or_creating_on_failure(self):
        self.source_a.action_prepare_processing_cron()
        cron = self.source_a.processing_cron_id
        before = self.env["ir.cron"].sudo().search_count([])
        group = self.env.ref("sales_team.group_sale_salesman_all_leads")
        try:
            self.operator_a.write({"groups_id": [Command.link(group.id)]})
            with unittest.TestCase.assertRaises(self, (UserError, AccessError, ValidationError)):
                self.source_a.action_prepare_processing_cron()
            self.env.flush_all()
            self.env.invalidate_all()
            self.assertFalse(cron.active)
            self.assertEqual(self.source_a.processing_cron_id, cron)
            self.assertEqual(self.env["ir.cron"].sudo().search_count([]), before)
        finally:
            self.operator_a.write({"groups_id": [Command.unlink(group.id)]})
            self.env.registry.clear_cache()

    def test_cron_preparation_rejects_linked_code_model_user_or_processor_mismatch(self):
        self.source_a.action_prepare_processing_cron()
        cron = self.source_a.processing_cron_id
        original = cron.read(["code", "model_id", "user_id", "active"])[0]
        before = self.env["ir.cron"].sudo().search_count([])
        for values in ({"code": "model.search([])"},
                       {"model_id": self.env["ir.model"]._get("res.partner").id},
                       {"user_id": self.other_operator.id}):
            cron.with_user(self.settings).write(values)
            with unittest.TestCase.assertRaises(self, (UserError, AccessError, ValidationError)):
                self.source_a.action_prepare_processing_cron()
            self.env.flush_all()
            self.env.invalidate_all()
            self.assertEqual(self.source_a.processing_cron_id, cron)
            self.assertEqual(self.env["ir.cron"].sudo().search_count([]), before)
            self.assertFalse(cron.active)
            cron.with_user(self.settings).write({
                "code": original["code"], "model_id": original["model_id"][0],
                "user_id": original["user_id"][0],
            })
        self.source_a.write({"processor_user_id": self.other_operator.id})
        with unittest.TestCase.assertRaises(self, (UserError, AccessError, ValidationError)):
            self.source_a.action_prepare_processing_cron()
        self.assertEqual(cron.user_id, self.operator_a)

    def test_private_cron_validation_reauthorizes_actual_source_and_exact_link(self):
        self.source_a.action_prepare_processing_cron()
        cron = self.source_a.processing_cron_id
        self.source_a._ui_validate_processing_cron(cron)
        before = cron.read(["code", "user_id", "model_id", "active"])
        for user in (self.integration_a, self.operator_a, self.sales_a, self.plain,
                     self.other_operator, self.operator_b):
            source = self.actor(self.SOURCE, user).sudo().with_context(
                import_settings=True, default_processor_user_id=self.operator_a.id,
            ).browse(self.source_a.id)
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                source._ui_validate_processing_cron(cron.sudo())
        with self.assertRaises(AccessError):
            self.actor(self.SOURCE, self.settings, self.company_b).sudo().browse(
                self.source_a.id,
            )._ui_validate_processing_cron(cron.sudo())
        other_source = self.source()
        other_source.action_prepare_processing_cron()
        with unittest.TestCase.assertRaises(self, (AccessError, UserError, ValidationError)):
            self.source_a._ui_validate_processing_cron(other_source.processing_cron_id.sudo())
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(cron.read(["code", "user_id", "model_id", "active"]), before)
        self.assertEqual(self.source_a.processing_cron_id, cron)

    def test_cron_native_server_action_runs_as_dedicated_processor_without_sudo(self):
        job = self.job(self.submit(), self.operator_a)
        self.source_a.action_prepare_processing_cron()
        cron = self.source_a.processing_cron_id
        action = cron.ir_actions_server_id.with_user(cron.user_id).with_context(
            allowed_company_ids=self.company_a.ids,
        )
        self.assertFalse(action.env.su)
        self.assertEqual(action.env.uid, self.operator_a.id)
        before = self.native_counts()
        action.run()
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.sale_order_id.create_uid, self.operator_a)
        self.assertEqual(self.attempts(job).actor_id, self.operator_a)
        self.assertEqual(self.native_counts()["sale.order"], before["sale.order"] + 1)
        self.assertFalse(cron.active)
