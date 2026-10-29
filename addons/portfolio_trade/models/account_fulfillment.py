"""Protect native customer invoice claims without replacing the accounting engine."""
import math

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare

from .account_capability import (
    accounting_frame,
    include_account_parents,
    native_frame,
    permits_state,
    state_action,
)
from .financial_policy import financial_facts, lock_financial, physical_facts, require_billing

RAW_LINE = {
    "balance", "debit", "credit", "amount_currency", "price_subtotal", "price_total",
    "currency_rate", "amount_residual", "amount_residual_currency", "parent_state",
    "tax_base_amount", "quantity_product_uom",
}
RAW_MOVE = {"amount_untaxed", "amount_tax", "amount_total", "amount_residual", "tax_totals", "invoice_currency_rate", "direction_sign"}
LINE_FACTS = RAW_LINE | {
    "move_id", "company_id", "product_id", "product_uom_id", "quantity", "price_unit",
    "discount", "tax_ids", "sale_line_ids", "display_type", "is_downpayment", "currency_id",
    "account_id", "date", "partner_id",
}
MOVE_FACTS = RAW_MOVE | {
    "line_ids", "invoice_line_ids", "state", "company_id", "currency_id", "move_type",
    "reversed_entry_id", "journal_id", "partner_id", "invoice_date", "date",
    "fiscal_position_id", "invoice_payment_term_id", "invoice_cash_rounding_id",
}


def _relation_commands(value):
    """Match native _RelationalMulti.write_batch before inspecting identities."""
    if value is False or value is None:
        return [(5, 0, 0)]
    if isinstance(value, models.BaseModel):
        return [(6, 0, value.ids)]
    if isinstance(value, tuple):
        return [(6, 0, value)]
    if isinstance(value, list) and value and not isinstance(value[0], (tuple, list)):
        return [(6, 0, value)]
    return value or []


def _relation_ids(commands):
    ids = set()
    for command in _relation_commands(commands):
        if command[0] == 6:
            ids.update(command[2])
        elif command[0] in (1, 2, 3, 4):
            ids.add(command[1])
    return ids


def _defaults(model, values):
    names = {
        "company_id", "move_id", "move_type", "state", "journal_id", "sale_line_ids",
        "line_ids", "invoice_line_ids", "reversed_entry_id", "account_id",
    } & set(model._fields)
    return {**model.default_get(sorted(names)), **values}


def _pending_source_orders(env, commands):
    """Resolve the real parent company before an inline SOL exists in the ORM."""
    orders = env["sale.order"]
    for command in _relation_commands(commands):
        if command[0] == 0:
            values = {**env["sale.order.line"].default_get(["order_id"]), **command[2]}
            orders |= env["sale.order"].browse(values.get("order_id") or [])
    return orders


def _scope(records, values_list=(), creating=False):
    """Bound current, candidate, defaults, nested and stolen-line identities."""
    env = records.env
    sources = env["sale.order.line"]
    moves = records if records._name == "account.move" else records.sudo().move_id
    entries = records if records._name == "account.move.line" else records.sudo().line_ids
    companies = moves.sudo().company_id | entries.sudo().company_id
    source_defaults = env["account.move.line"].default_get(["sale_line_ids", "account_id"])
    pending_orders = env["sale.order"]
    for supplied in values_list:
        values = _defaults(records, supplied) if creating else supplied
        companies |= env["res.company"].browse(values.get("company_id") or [])
        journal = env["account.journal"].browse(values.get("journal_id") or []).sudo()
        companies |= journal.company_id
        account = env["account.account"].browse(values.get("account_id") or []).sudo()
        companies |= account.company_ids
        moves |= env["account.move"].browse(values.get("move_id") or [])
        moves |= env["account.move"].browse(values.get("reversed_entry_id") or [])
        if records._name == "account.move.line":
            sources |= env["sale.order.line"].browse(_relation_ids(values.get("sale_line_ids")))
            pending_orders |= _pending_source_orders(env, values.get("sale_line_ids"))
        for field in ("line_ids", "invoice_line_ids"):
            for command in _relation_commands(values.get(field, [])):
                if command[0] in (1, 2, 3, 4):
                    entries |= env["account.move.line"].browse(command[1])
                elif command[0] == 6:
                    entries |= env["account.move.line"].browse(command[2])
                if command[0] in (0, 1):
                    # Only CREATE applies account/source defaults. UPDATE keeps
                    # its current canonical identities unless explicitly edited.
                    nested = {**source_defaults, **command[2]} if command[0] == 0 else command[2]
                    sources |= env["sale.order.line"].browse(_relation_ids(nested.get("sale_line_ids")))
                    pending_orders |= _pending_source_orders(env, nested.get("sale_line_ids"))
                    companies |= env["res.company"].browse(nested.get("company_id") or [])
                    companies |= env["account.account"].browse(nested.get("account_id") or []).sudo().company_ids
                    moves |= env["account.move"].browse(nested.get("move_id") or [])
    moves |= entries.sudo().move_id
    originals = moves.sudo().reversed_entry_id
    sources |= (entries.sudo() | moves.sudo().line_ids | originals.line_ids).sale_line_ids
    companies |= moves.sudo().company_id | originals.company_id | sources.sudo().order_id.company_id
    pending_companies = pending_orders.sudo().company_id
    if pending_orders and (_protected(sources) or any(pending_companies.mapped("portfolio_fulfillment_enabled"))):
        # Existing quotations and sale lines are the financial source; invoice
        # mutations do not create that business source as a side effect. Resolve
        # and authorize it before creating any header, AML or SOL, including in
        # source-default and nested-write paths.
        require_billing(env, companies | pending_companies)
        pending_orders.with_user(env.uid).check_access("read")
        raise UserError(env._("Create the sales source line before invoicing; inline sales source creation is not supported."))
    return sources, moves, companies


def _protected(sources):
    return sources.filtered(lambda source: source.sudo().order_id.company_id.portfolio_fulfillment_enabled)


def _supplied(model, values, fields, creating=False):
    result = fields.intersection(values)
    if creating:
        result |= fields.intersection(model.env["ir.default"]._get_model_defaults(model._name))
        result |= {name for name in fields if f"default_{name}" in model.env.context}
    return result


def _preflight(records, values_list, creating=False):
    env = records.env
    raw = RAW_MOVE if records._name == "account.move" else RAW_LINE
    for values in values_list:
        if _supplied(records, values, raw, creating):
            raise UserError(env._("Invoice accounting amounts are derived from the native quantity, price and taxes."))
        if creating and records._name == "account.move.line":
            effective = _defaults(records, values)
            parent = env["account.move"].browse(effective.get("move_id") or []).sudo()
            if parent.state == "posted":
                raise UserError(env._("Public invoice line creation cannot append facts to a posted invoice."))
        if records._name == "account.move":
            effective = _defaults(records, values) if creating else values
            if creating and effective.get("state", "draft") != "draft":
                raise UserError(env._("Create a draft invoice and use its native posting action."))
            if "state" in values and not permits_state(records, values["state"]):
                raise UserError(env._("Use the native invoice posting, draft or cancellation action."))
            line_model = records.env["account.move.line"]
            for name in ("line_ids", "invoice_line_ids"):
                for command in _relation_commands(effective.get(name, [])):
                    if command[0] in (0, 1) and _supplied(line_model, command[2], RAW_LINE, command[0] == 0):
                        raise UserError(env._("Invoice line amounts must be computed by native accounting."))
                    linked = line_model.browse(
                        command[2] if command[0] == 6 else [command[1]] if command[0] in (1, 2, 3, 4) else [],
                    ).sudo()
                    if linked.filtered(lambda line: line.move_id.state == "posted"):
                        raise UserError(env._("Posted invoice lines cannot be edited or moved through another invoice."))
                    if command[0] == 1:
                        parent = env["account.move"].browse(command[2].get("move_id") or []).sudo()
                        if parent.state == "posted":
                            raise UserError(env._("Invoice line commands cannot attach facts to a posted invoice."))
                    if command[0] == 1 and "sale_line_ids" in command[2] and linked.sale_line_ids:
                        if not _resulting_ids(linked.sale_line_ids.ids, command[2]["sale_line_ids"]):
                            raise UserError(env._("Retain the sales source on a draft invoice line, or delete the claim."))
        if not creating:
            moves = records if records._name == "account.move" else records.sudo().move_id
            if records._name == "account.move.line":
                moves |= env["account.move"].browse(values.get("move_id") or []).sudo()
            facts = MOVE_FACTS if records._name == "account.move" else LINE_FACTS
            if facts.intersection(values) and moves.filtered(lambda move: move.state == "posted"):
                if not (set(values) <= {"state"} and permits_state(moves, values.get("state"))):
                    raise UserError(env._("Posted sales invoice facts are immutable; reset through the native action."))


def _resulting_ids(current_ids, commands):
    ids = set(current_ids)
    for command in _relation_commands(commands):
        if command[0] == 6:
            ids = set(command[2])
        elif command[0] == 5:
            ids.clear()
        elif command[0] == 4:
            ids.add(command[1])
        elif command[0] in (2, 3):
            ids.discard(command[1])
    return ids


def _pricing_key(entry, source, original):
    return (
        original.id, source.id, entry.product_id.id, entry.product_uom_id.id,
        entry.price_unit, entry.discount, tuple(sorted(entry.tax_ids.ids)), entry.currency_id.id,
    )


def _validate_credit_budgets(sources, financial, physical):
    """Both gross budgets and each original pricing group reserve draft claims."""
    env = sources.env
    credits = env["account.move.line"].sudo().search([
        ("sale_line_ids", "in", sources.ids), ("display_type", "=", "product"),
        ("move_id.move_type", "=", "out_refund"), ("move_id.state", "!=", "cancel"),
    ])
    billed = {}
    claimed = {}
    physical_sources = sources.filtered(lambda source: source.id in financial)
    for source in physical_sources:
        facts = financial[source.id]
        if float_compare(facts["credit_reserved"], physical[source.id]["returned"], precision_rounding=source.product_uom.rounding) > 0:
            raise UserError(env._("Credit quantities exceed completed, uncredited customer returns."))
        for original in facts["originals"].values():
            if float_compare(original["credit_reserved"], original["invoice_posted"], precision_rounding=source.product_uom.rounding) > 0:
                raise UserError(env._("Credit quantities exceed the selected original posted invoice."))
    for original in credits.move_id.reversed_entry_id:
        for entry in original.invoice_line_ids.filtered(lambda line: line.display_type == "product"):
            for source in entry.sale_line_ids & physical_sources:
                key = _pricing_key(entry, source, original)
                billed[key] = billed.get(key, 0) + entry.product_uom_id._compute_quantity(entry.quantity, source.product_uom, round=False)
    for entry in credits:
        original = entry.move_id.reversed_entry_id
        if (
            original.state != "posted" or original.move_type != "out_invoice"
            or entry.move_id.company_id != original.company_id
            or entry.move_id.currency_id != original.currency_id
            or entry.currency_id != entry.move_id.currency_id
        ):
            raise UserError(env._("A credit requires its original posted customer invoice in the same company and currency."))
        for source in entry.sale_line_ids & physical_sources:
            key = _pricing_key(entry, source, original)
            claimed[key] = claimed.get(key, 0) + entry.product_uom_id._compute_quantity(entry.quantity, source.product_uom, round=False)
            if key not in billed or float_compare(claimed[key], billed[key], precision_rounding=source.product_uom.rounding) > 0:
                raise UserError(env._("Credit quantity and pricing must remain attributable to the original invoice lines."))


def _quantities(moves):
    values = {}
    active = moves.sudo().filtered(lambda move: move.state != "cancel")
    for entry in active.invoice_line_ids.filtered(lambda line: line.display_type == "product"):
        for source in entry.sale_line_ids:
            if entry.product_uom_id.category_id == source.product_uom.category_id:
                values[entry.id] = (source.id, entry.product_uom_id._compute_quantity(entry.quantity, source.product_uom, round=False))
    return values


def _validate(moves, sources, before=None, posting=False):
    env = moves.env
    protected = _protected(sources)
    if not protected:
        return
    # Counterpart/tax synchronization must have finished before any final check.
    moves.env.flush_all()
    affected = moves.sudo().filtered(lambda move: move.state != "cancel" and move.line_ids.sale_line_ids & protected)
    for move in affected:
        if move.move_type not in ("out_invoice", "out_refund"):
            raise UserError(env._("Controlled sales sources require customer invoices or credit notes."))
        if move.currency_id != move.company_id.currency_id:
            raise UserError(env._("Controlled fulfillment invoices currently require the company currency."))
        for entry in move.line_ids:
            commercial = entry.sale_line_ids.filtered(
                lambda source: not source.display_type and source.product_id.type != "combo",
            )
            if commercial and entry.display_type != "product":
                raise UserError(env._("Commercial sales invoice lines must retain their product classification."))
        for entry in move.invoice_line_ids.filtered(lambda line: line.display_type == "product"):
            linked = entry.sale_line_ids
            if (len(linked) != 1 or linked.company_id != move.company_id or entry.company_id != move.company_id
                or entry.currency_id != move.currency_id):
                raise UserError(env._("Each invoice product line requires one consistent sales source."))
            if (
                entry.product_id != linked.product_id or not entry.product_uom_id
                or entry.product_uom_id.category_id != linked.product_uom.category_id
                or not math.isfinite(entry.quantity) or entry.quantity < 0
                or not math.isfinite(entry.price_unit) or not math.isfinite(entry.discount)
            ):
                raise UserError(env._("Invoice product, quantity and unit must match the actual sales source."))
        if move._get_unbalanced_moves({"records": move}):
            raise UserError(env._("The sales invoice must remain balanced."))
        base_lines, _tax_lines = move._get_rounded_base_and_tax_lines(round_from_tax_lines=False)
        # For the two supported customer move types this is the native formula,
        # derived from canonical move_type rather than a writable computed cache.
        direction = 1 if move.move_type == "out_refund" else -1
        for base in base_lines:
            entry = base["record"]
            details = base["tax_details"]
            if (
                not move.currency_id.is_zero(entry.amount_currency - direction * details["total_excluded_currency"])
                or not move.company_id.currency_id.is_zero(entry.balance - direction * details["total_excluded"])
                or not move.currency_id.is_zero(entry.price_subtotal - details["raw_total_excluded_currency"])
                or not move.currency_id.is_zero(entry.price_total - details["raw_total_included_currency"])
            ):
                raise UserError(env._("Sales invoice amounts must match their native quantity, price and taxes."))
    financial = financial_facts(protected.with_env(moves.env))
    physical = physical_facts(protected.with_env(moves.env))
    if any(facts["invalid_line_ids"] for facts in financial.values()):
        raise UserError(env._("Malformed active invoice facts prevent granting a financial allowance."))
    _validate_credit_budgets(protected.with_env(moves.env), financial, physical)
    normal = affected.filtered(lambda move: move.move_type == "out_invoice")
    now = _quantities(normal)
    posting_sources = normal.invoice_line_ids.sale_line_ids.ids if posting else []
    increased = set()
    for line_id, (source_id, quantity) in now.items():
        old_source, old_quantity = (before or {}).get(line_id, (None, 0))
        if old_source != source_id or quantity > old_quantity:
            increased.add(source_id)
    for source in protected:
        if source.id not in financial:
            continue  # Native service invoices have no physical quota.
        facts = financial[source.id]
        if source.id in increased or source.id in posting_sources:
            net = physical[source.id]["delivered"] - physical[source.id]["returned"]
            if float_compare(facts["signed_reserved"], net, precision_rounding=source.product_uom.rounding) > 0:
                raise UserError(env._("The invoice quantity exceeds delivered, unreserved sales quantity."))


def _run_action(moves, target, operation):
    sources, related, companies = _scope(moves)
    if not _protected(sources):
        return operation()
    require_billing(moves.env, companies)
    with moves.env.cr.savepoint():
        lock_financial(sources, extra_companies=companies)
        before = _quantities(moves)
        with accounting_frame(moves.env, companies, related), state_action(moves, target):
            result = operation()
            _validate(moves, sources, before=before, posting=target == "posted")
            return result


class AccountMove(models.Model):
    _inherit = "account.move"

    @api.model_create_multi
    def create(self, vals_list):
        sources, related, companies = _scope(self, vals_list, creating=True)
        if not _protected(sources):
            return super().create(vals_list)
        require_billing(self.env, companies)
        _preflight(self, vals_list, creating=True)
        with self.env.cr.savepoint():
            lock_financial(sources, extra_companies=companies)
            with accounting_frame(self.env, companies, related, new_moves=len(vals_list)) as frame:
                moves = super().create(vals_list)
                frame.include_created_parents(moves)
                _validate(moves, sources)
            include_account_parents(moves)
            return moves

    def write(self, values):
        if native_frame(self.env, self):
            if "state" in values and not permits_state(self, values["state"]):
                raise UserError(_("The native invoice state action does not authorize this transition."))
            return super().write(values)
        sources, related, companies = _scope(self, [values])
        if not _protected(sources):
            return super().write(values)
        require_billing(self.env, companies)
        _preflight(self, [values])
        with self.env.cr.savepoint():
            lock_financial(sources, extra_companies=companies)
            before = _quantities(related | self)
            with accounting_frame(self.env, companies, related):
                result = super().write(values)
                _validate(related | self, sources, before=before)
                return result

    def copy_data(self, default=None):
        # Native reversal/copy prepares semantic invoice input. Copied writable
        # computed values must be recomputed, never interpreted as public input.
        values_list = super().copy_data(default=default)
        for move, values in zip(self, values_list):
            if not _protected(move.sudo().line_ids.sale_line_ids):
                continue
            if RAW_MOVE.intersection(default or {}):
                raise UserError(self.env._("Explicit copied invoice amounts must still be computed by native accounting."))
            for name in ("line_ids", "invoice_line_ids"):
                for command in (default or {}).get(name) or []:
                    if command[0] in (0, 1) and RAW_LINE.intersection(command[2]):
                        raise UserError(self.env._("Explicit copied line amounts must still be computed by native accounting."))
            for name in RAW_MOVE:
                values.pop(name, None)
            for name in ("line_ids", "invoice_line_ids"):
                for command in values.get(name) or []:
                    if command[0] == 0:
                        for field in RAW_LINE:
                            command[2].pop(field, None)
        return values_list

    def _reverse_moves(self, default_values_list=None, cancel=False):
        sources, related, companies = _scope(self)
        if not _protected(sources):
            return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)
        require_billing(self.env, companies)
        with self.env.cr.savepoint():
            lock_financial(sources, extra_companies=companies)
            before = _quantities(related)
            with accounting_frame(self.env, companies, related, new_moves=len(self)) as frame:
                reverse = super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)
                frame.include_created_parents(reverse)
                _validate(related | reverse, sources, before=before)
                return reverse

    def unlink(self):
        sources, related, companies = _scope(self)
        if not _protected(sources):
            return super().unlink()
        require_billing(self.env, companies)
        if any(move.state == "posted" for move in self):
            raise UserError(_("Reset posted invoices through the native action before deleting them."))
        with self.env.cr.savepoint():
            lock_financial(sources, extra_companies=companies)
            with accounting_frame(self.env, companies, related):
                return super().unlink()

    def action_post(self):
        return _run_action(self, "posted", lambda: super(AccountMove, self).action_post())

    def _post(self, soft=True):
        # Cron and private native accounting callers still use the same boundary.
        if permits_state(self, "posted"):
            return super()._post(soft=soft)
        return _run_action(self, "posted", lambda: super(AccountMove, self)._post(soft=soft))

    def button_draft(self):
        return _run_action(self, "draft", lambda: super(AccountMove, self).button_draft())

    def button_cancel(self):
        return _run_action(self, "cancel", lambda: super(AccountMove, self).button_cancel())


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    @api.model_create_multi
    def create(self, vals_list):
        parents = self.env["account.move"].browse({values["move_id"] for values in vals_list if values.get("move_id")})
        include_account_parents(parents)
        if native_frame(self.env, parents):
            return super().create(vals_list)
        sources, related, companies = _scope(self, vals_list, creating=True)
        if not _protected(sources):
            return super().create(vals_list)
        require_billing(self.env, companies)
        _preflight(self, vals_list, creating=True)
        with self.env.cr.savepoint():
            lock_financial(sources, extra_companies=companies)
            before = _quantities(related)
            with accounting_frame(self.env, companies, related):
                entries = super().create(vals_list)
                _validate(entries.move_id, sources, before=before)
                return entries

    def write(self, values):
        if native_frame(self.env, self.sudo().move_id):
            return super().write(values)
        sources, related, companies = _scope(self, [values])
        if not _protected(sources):
            return super().write(values)
        require_billing(self.env, companies)
        _preflight(self, [values])
        if "sale_line_ids" in values and any(
            entry.sale_line_ids and not _resulting_ids(entry.sale_line_ids.ids, values["sale_line_ids"])
            for entry in self.sudo()
        ):
            raise UserError(_("Retain the sales source on an invoice line; delete a draft claim to release it."))
        with self.env.cr.savepoint():
            lock_financial(sources, extra_companies=companies)
            before = _quantities(related)
            with accounting_frame(self.env, companies, related):
                result = super().write(values)
                _validate(related | self.move_id, sources, before=before)
                return result

    def unlink(self):
        parents = self.sudo().move_id
        if native_frame(self.env, parents):
            return super().unlink()
        sources, related, companies = _scope(self)
        if not _protected(sources):
            return super().unlink()
        require_billing(self.env, companies)
        if any(move.state == "posted" for move in parents):
            raise UserError(_("Posted invoice lines cannot be deleted."))
        with self.env.cr.savepoint():
            lock_financial(sources, extra_companies=companies)
            with accounting_frame(self.env, companies, related):
                return super().unlink()


class AccountMoveReversal(models.TransientModel):
    _inherit = "account.move.reversal"

    def _prepare_default_reversal(self, move):
        values = super()._prepare_default_reversal(move)
        if _protected(move.sudo().line_ids.sale_line_ids):
            # Native CNY reversal preparation copies this computed field; let
            # native company-currency computation derive it on the new draft.
            values.pop("invoice_currency_rate", None)
        return values

    def reverse_moves(self, is_modify=False):
        sources, _related, companies = _scope(self.move_ids)
        if not _protected(sources):
            return super().reverse_moves(is_modify=is_modify)
        require_billing(self.env, companies)
        with self.env.cr.savepoint():
            lock_financial(sources, extra_companies=companies)
            return super().reverse_moves(is_modify=is_modify)
