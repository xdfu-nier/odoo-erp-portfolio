"""Public configuration operations always use the actual actor and company scope."""

import unicodedata

from odoo import _, api
from odoo.exceptions import AccessError, ValidationError
from odoo.models import regex_field_agg
from odoo.osv import expression

SETTINGS = "base.group_system"
INTEGRATION = "portfolio_order_bridge.group_import_integration"
OPERATOR = "portfolio_order_bridge.group_import_operator"
SALES = "sales_team.group_sale_salesman"
CONFIG_GROUPS = f"{SETTINGS},{OPERATOR}"
FORBIDDEN_GROUPS = (
    SETTINGS, "base.group_erp_manager", "sales_team.group_sale_manager",
    "purchase.group_purchase_user", "stock.group_stock_user", "account.group_account_invoice",
)


def safe_text(env, value, label, maximum=64):
    """Validate before PostgreSQL sees NULs or unencodable surrogate characters."""
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > maximum
        or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)
    ):
        raise ValidationError(env._(
            "%(label)s must be clean, nonempty text of at most %(maximum)s characters.",
            label=label, maximum=maximum,
        ))


class ImportAccess:
    """Shared boundaries; private ORM operations remain usable by trusted validators."""

    __slots__ = ()

    def _import_actor(self):
        actor = self.with_user(self.env.uid)
        user = actor.env.user.sudo()
        if not user.active or user.share:
            raise AccessError(_("Import configuration requires an active internal user."))
        actual = user.company_ids.ids
        scoped = self.env.context.get("allowed_company_ids") or user.company_id.ids
        if not isinstance(scoped, (list, tuple)) or any(
            type(company_id) is not int or company_id not in actual for company_id in scoped
        ):
            raise AccessError(_("Import configuration is limited to your actual allowed companies."))
        # Retain explicit native scope and read options; discard caller-supplied defaults,
        # force_company and application flags before any ORM operation can consume them.
        context = {
            key: value for key, value in self.env.context.items()
            if key in {"lang", "tz", "active_test", "bin_size", "prefetch_fields"}
        }
        context["allowed_company_ids"] = list(scoped)
        return actor.with_context(context)

    def _import_settings_actor(self):
        actor = self._import_actor()
        if not actor.env.user.has_group(SETTINGS):
            raise AccessError(_("Only Settings users may maintain import configuration."))
        return actor

    def _import_authorize_company(self, company_id):
        if type(company_id) is not int or company_id not in self.env.companies.ids:
            raise AccessError(_("The import company must be in your active company scope."))

    def _import_read_actor(self):
        actor = self._import_actor()
        actor.check_access("read")
        return actor

    @api.private
    @api.returns("self")
    def exists(self):
        # The native helper bypasses ACLs; retain it for internal validation only.
        return super().exists()

    def _import_check_query_fields(self, domain=None, field_specs=(), order=None):
        # Native SQL field checks miss optimized M2M predicates and ignore some
        # read_group field specs. Check caller field names before native evaluation.
        names = {
            term[0].split(".", 1)[0]
            for term in expression.normalize_domain(domain or [])
            if expression.is_leaf(term) and isinstance(term[0], str)
        }
        for spec in field_specs:
            match = regex_field_agg.match(spec)
            if match:
                name, _function, field = match.groups()
                names.add(field or name)
        if order:
            names.update(term.split()[0].split(".", 1)[0] for term in order.split(",") if term.strip())
        self.check_field_access_rights("read", names.intersection(self._fields))

    def read(self, fields=None, load="_classic_read"):
        actor = self._import_read_actor()
        return super(ImportAccess, actor).read(fields=fields, load=load)

    def web_read(self, specification):
        # Native id-only web_read bypasses read(), so it needs its own boundary.
        actor = self._import_read_actor()
        return super(ImportAccess, actor).web_read(specification)

    @api.model
    def search(self, domain, offset=0, limit=None, order=None):
        actor = self._import_actor()
        return super(ImportAccess, actor).search(domain, offset=offset, limit=limit, order=order)

    @api.model
    def search_fetch(self, domain, field_names, offset=0, limit=None, order=None):
        actor = self._import_actor()
        actor._import_check_query_fields(domain, field_names, order)
        return super(ImportAccess, actor).search_fetch(
            domain, field_names, offset=offset, limit=limit, order=order,
        )

    @api.model
    def search_count(self, domain, limit=None):
        actor = self._import_actor()
        actor._import_check_query_fields(domain)
        return super(ImportAccess, actor).search_count(domain, limit=limit)

    @api.model
    def search_read(self, domain=None, fields=None, offset=0, limit=None, order=None, **read_kwargs):
        actor = self._import_actor()
        return super(ImportAccess, actor).search_read(
            domain=domain, fields=fields, offset=offset, limit=limit, order=order, **read_kwargs,
        )

    @api.model
    def read_group(self, domain, fields, groupby, offset=0, limit=None, orderby=False, lazy=True):
        actor = self._import_actor()
        groups = [groupby] if isinstance(groupby, str) else list(groupby)
        actor._import_check_query_fields(domain, [*fields, *groups], orderby)
        return super(ImportAccess, actor).read_group(
            domain, fields, groupby, offset=offset, limit=limit, orderby=orderby, lazy=lazy,
        )

    def export_data(self, fields_to_export):
        actor = self._import_read_actor()
        actor.check_field_access_rights("read", [path.split("/", 1)[0] for path in fields_to_export])
        return super(ImportAccess, actor).export_data(fields_to_export)

    @api.model
    def fields_get(self, allfields=None, attributes=None):
        actor = self._import_actor()
        return super(ImportAccess, actor).fields_get(allfields=allfields, attributes=attributes)

    @api.model
    def name_search(self, name="", args=None, operator="ilike", limit=100):
        actor = self._import_actor()
        actor._import_check_query_fields(args)
        return super(ImportAccess, actor).name_search(name=name, args=args, operator=operator, limit=limit)

    @api.model_create_multi
    def create(self, vals_list):
        actor = self._import_settings_actor()
        # Own the entire batch's rollback: callers may catch errors and continue.
        with actor.env.cr.savepoint():
            values = [actor._import_prepare_create(dict(vals)) for vals in vals_list]
            records = super(ImportAccess, actor).create(values)
            records._import_validate()
            return records

    def write(self, vals):
        actor = self._import_settings_actor()
        with actor.env.cr.savepoint():
            actor.check_access("write")
            values = actor._import_prepare_write(dict(vals))
            result = super(ImportAccess, actor).write(values)
            actor._import_validate()
            return result

    def copy(self, default=None):
        raise AccessError(_("Import configuration cannot be copied; create an explicit new record."))

    def copy_data(self, default=None):
        raise AccessError(_("Import configuration cannot be copied; create an explicit new record."))

    def unlink(self):
        raise AccessError(_("Import configuration cannot be deleted; archive it instead."))
