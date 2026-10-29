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
RAW_MOVE = {"amount_untaxed", "amount_tax", "amount_total", "amount_residual", "tax_totals", "invoice_currency_rate"}
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


def _relation_ids(commands):
    ids = set()
    for command in commands or []:
        if isinstance(command, int):
            ids.add(command)
        elif command[0] == 6:
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


def _scope(records, values_list=(), creating=False):
    """Bound current, candidate, defaults, nested and stolen-line identities."""
    env = records.env
    sources = env["sale.order.line"]
    moves = records if records._name == "account.move" else records.sudo().move_id
    entries = records if records._name == "account.move.line" else records.sudo().line_ids
    companies = moves.sudo().company_id | entries.sudo().company_id
    source_defaults = env["account.move.line"].default_get(["sale_line_ids"])
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
        for field in ("line_ids", "invoice_line_ids"):
            for command in values.get(field) or []:
                if command[0] in (1, 2, 3, 4):
                    entries |= env["account.move.line"].browse(command[1])
                elif command[0] == 6:
                    entries |= env["account.move.line"].browse(command[2])
                if command[0] in (0, 1):
                    nested = {**source_defaults, **command[2]}
                    sources |= env["sale.order.line"].browse(_relation_ids(nested.get("sale_line_ids")))
                    companies |= env["res.company"].browse(nested.get("company_id") or [])
                    companies |= env["account.account"].browse(nested.get("account_id") or []).sudo().company_ids
                    moves |= env["account.move"].browse(nested.get("move_id") or [])
    moves |= entries.sudo().move_id
    originals = moves.sudo().reversed_entry_id
    sources |= (entries.sudo() | moves.sudo().line_ids | originals.line_ids).sale_line_ids
    companies |= moves.sudo().company_id | originals.company_id | sources.sudo().order_id.company_id
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
        if records._name == "account.move":
            effective = _defaults(records, values) if creating else values
            if creating and effective.get("state", "draft") != "draft":
                raise UserError(env._("Create a draft invoice and use its native posting action."))
            if "state" in values and not permits_state(records, values["state"]):
                raise UserError(env._("Use the native invoice posting, draft or cancellation action."))
            line_model = records.env["account.move.line"]
            for name in ("line_ids", "invoice_line_ids"):
                for command in effective.get(name) or []:
                    if command[0] in (0, 1) and _supplied(line_model, command[2], RAW_LINE, command[0] == 0):
                        raise UserError(env._("Invoice line amounts must be computed by native accounting."))
        if not creating:
            moves = records if records._name == "account.move" else records.sudo().move_id
            facts = MOVE_FACTS if records._name == "account.move" else LINE_FACTS
            if facts.intersection(values) and moves.filtered(lambda move: move.state == "posted"):
                if not (set(values) <= {"state"} and permits_state(moves, values.get("state"))):
                    raise UserError(env._("Posted sales invoice facts are immutable; reset through the native action."))


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
    affected = moves.sudo().filtered(lambda move: move.line_ids.sale_line_ids & protected)
    for move in affected:
        if move.move_type not in ("out_invoice", "out_refund"):
            raise UserError(env._("Controlled sales sources require customer invoices or credit notes."))
        if move.currency_id != move.company_id.currency_id:
            raise UserError(env._("Controlled fulfillment invoices currently require the company currency."))
        for entry in move.invoice_line_ids.filtered(lambda line: line.display_type == "product"):
            linked = entry.sale_line_ids
            if len(linked) != 1 or linked.company_id != move.company_id or entry.company_id != move.company_id:
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
        for base in base_lines:
            entry = base["record"]
            details = base["tax_details"]
            if (
                not move.currency_id.is_zero(entry.amount_currency - move.direction_sign * details["total_excluded_currency"])
                or not move.company_id.currency_id.is_zero(entry.balance - move.direction_sign * details["total_excluded"])
                or not move.currency_id.is_zero(entry.price_subtotal - details["raw_total_excluded_currency"])
                or not move.currency_id.is_zero(entry.price_total - details["raw_total_included_currency"])
            ):
                raise UserError(env._("Sales invoice amounts must match their native quantity, price and taxes."))
    financial = financial_facts(protected.with_env(moves.env))
    physical = physical_facts(protected.with_env(moves.env))
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
        if facts["invalid_line_ids"]:
            raise UserError(env._("Malformed active invoice facts prevent granting a financial allowance."))
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
            before = _quantities(self)
            with accounting_frame(self.env, companies, related):
                result = super().write(values)
                _validate(self, sources, before=before)
                return result

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
        if "sale_line_ids" in values and not _relation_ids(values["sale_line_ids"]):
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
