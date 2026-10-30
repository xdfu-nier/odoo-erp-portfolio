"""Authoritative, read-only fulfillment facts and the shared financial lock.

Both fact APIs are keyed by physical sale.order.line ID; all quantities are in
that source's UoM, converted by native UoM methods without rounding quantities
upwards. Services have no physical quantity budget and are omitted. Callers must
use source-UoM precision when comparing allowances.

physical_facts returns separate gross ``delivered`` and genuine ``returned``
quantities from completed stock operations. A customer's completed return with
an inconsistent source/origin fails closed. ``to_refund`` and presentation or
accrual contexts cannot conceal an actual physical return.

financial_facts returns gross ``invoice_reserved`` / ``credit_reserved`` (draft
plus posted, never cancelled), their ``*_posted`` subsets, and ``signed_reserved``
and ``signed_posted`` (normal minus credit). ``originals`` maps original invoice
IDs to state, move_type, company_id and the same four gross quantities *for this
source only*. This supplies two independent credit budgets: total physical
returns minus source-global credits, and original posted invoices minus credits
against that selected original. Facts do not enforce either budget.

``invalid_line_ids`` identifies active malformed product AMLs: ambiguous source,
wrong product/company/UoM/quantity or missing/inconsistent original invoice.
Ambiguous or malformed identities never contribute a quantity. A credit with a
valid source/quantity still reserves its source and selected original even when
that original is invalid. A consumer granting an allowance must reject invalid
facts before using signed totals; these functions themselves perform no business
mutation or financial policy enforcement.
"""
import math

from odoo import _
from odoo.exceptions import AccessError, UserError

from .stock_policy import authorize_companies, companies_for


def _authorized_sources(lines):
    """Check the original uid's source ACL/rules before any elevated fact read."""
    actor = lines.with_user(lines.env.uid)
    actor.check_access("read")
    orders = actor.order_id
    orders.check_access("read")
    authorize_companies(orders.company_id)
    # Preserve precisely the active companies we have just authorized; discard
    # caller presentation/default contexts, especially accrual_entry_date.
    return actor.with_context(
        {"allowed_company_ids": actor.env.companies.ids, "active_test": False},
    )


def _physical_sources(lines):
    return lines.filtered(lambda line: not line.display_type and line.product_id.type == "consu" and not line.is_downpayment)


def require_billing(env, companies):
    """Require the real Billing group and actual/active companies, even in sudo."""
    actor = env["res.users"].with_user(env.uid).env.user
    authorize_companies(companies.with_env(actor.env))
    if not actor.has_group("account.group_account_invoice"):
        raise AccessError(_("Only Billing users can change sales invoices and credit notes."))


def _outgoing(location):
    # Native customer/intercompany customer boundaries, without any dated SOL
    # helper or cached delivered method/counter.
    return location._is_outgoing()


def _stock_quantity(move, source, returning=False):
    """Read actual SML quantities, validating their canonical parent identity."""
    company = source.order_id.company_id
    locations = move.location_id | move.location_dest_id
    if (
        move.state != "done" or move.product_id != source.product_id
        or move.company_id != company or move.sale_line_id != source
        or move.product_uom.category_id != source.product_uom.category_id
        or move.scrapped or move.is_inventory
        or move.picking_id and move.picking_id.company_id != company
        or move.picking_type_id and move.picking_type_id.company_id != company
        or locations.company_id - company
    ):
        raise UserError(_("Completed stock facts have an inconsistent sales source identity."))
    quantity = 0.0
    for detail in move.move_line_ids:
        if (
            detail.state != "done" or detail.product_id != source.product_id
            or detail.company_id != company or detail.move_id != move
            or detail.picking_id != move.picking_id
            or detail.product_uom_id.category_id != source.product_uom.category_id
            or (detail.location_id | detail.location_dest_id).company_id - company
            or not math.isfinite(detail.quantity) or detail.quantity < 0
            or bool(_outgoing(detail.location_id)) != returning
            or bool(_outgoing(detail.location_dest_id)) == returning
        ):
            raise UserError(_("Completed stock operation lines do not match their physical source."))
        quantity += detail.product_uom_id._compute_quantity(detail.quantity, source.product_uom, round=False)
    if not math.isfinite(quantity):
        raise UserError(_("Completed physical quantities must be finite."))
    return quantity


def physical_facts(lines):
    """Gross deliveries and genuine returns; never cached native SOL counters."""
    sources = _physical_sources(_authorized_sources(lines)).sudo()
    facts = {source.id: {"delivered": 0.0, "returned": 0.0} for source in sources}
    moves = sources.env["stock.move"].search([
        ("sale_line_id", "in", sources.ids), ("state", "=", "done"),
    ]) if sources else sources.env["stock.move"]
    for move in moves:
        source = move.sale_line_id
        outgoing = _outgoing(move.location_dest_id) and not _outgoing(move.location_id)
        incoming = _outgoing(move.location_id) and not _outgoing(move.location_dest_id)
        if move.scrapped or move.is_inventory or not (outgoing or incoming):
            continue
        if outgoing:
            # A legacy unrelated product/company cannot manufacture delivery
            # allowance for this source. Customer returns instead fail closed.
            if move.product_id != source.product_id or move.company_id != source.order_id.company_id:
                continue
            facts[source.id]["delivered"] += _stock_quantity(move, source)
        else:
            origin = move.origin_returned_move_id
            if not origin or not _outgoing(origin.location_dest_id) or _outgoing(origin.location_id):
                raise UserError(_("A completed customer return requires its genuine original delivery."))
            _stock_quantity(origin, source)
            facts[source.id]["returned"] += _stock_quantity(move, source, returning=True)
    return facts


def _account_lines(sources):
    """A bounded native-source query, independent of SOL invoice relation caches."""
    return sources.env["account.move.line"].sudo().search([
        ("sale_line_ids", "in", sources.ids),
        ("display_type", "=", "product"),
        ("move_id.move_type", "in", ["out_invoice", "out_refund"]),
    ]) if sources else sources.env["account.move.line"]


def _authorize_accounts(sources, entries):
    # Elevation is limited to the queried records' canonical identities. Check
    # every discovered company and attached source as the actor before reading
    # quantities or taking technical locks.
    moves = entries.move_id | entries.move_id.reversed_entry_id
    companies = moves.company_id | entries.company_id
    authorize_companies(companies.with_env(sources.env))
    if entries.sale_line_ids:
        _authorized_sources(entries.sale_line_ids.with_env(sources.env))
    return moves


def _original_facts(move):
    return {
        "state": move.state, "move_type": move.move_type, "company_id": move.company_id.id,
        "invoice_reserved": 0.0, "invoice_posted": 0.0,
        "credit_reserved": 0.0, "credit_posted": 0.0,
    }


def _invoice_quantity(entry, source):
    if (
        len(entry.sale_line_ids) != 1 or entry.sale_line_ids != source
        or entry.product_id != source.product_id or entry.is_downpayment
        or entry.company_id != source.order_id.company_id
        or entry.move_id.company_id != source.order_id.company_id
        or not entry.product_uom_id
        or entry.product_uom_id.category_id != source.product_uom.category_id
        or not math.isfinite(entry.quantity) or entry.quantity < 0
    ):
        return None
    quantity = entry.product_uom_id._compute_quantity(entry.quantity, source.product_uom, round=False)
    return quantity if math.isfinite(quantity) else None


def financial_facts(lines):
    """Gross reservations, posting subsets, selected-original budgets and defects."""
    sources = _physical_sources(_authorized_sources(lines))
    entries = _account_lines(sources)
    _authorize_accounts(sources, entries)
    facts = {source.id: {
        "invoice_reserved": 0.0, "invoice_posted": 0.0,
        "credit_reserved": 0.0, "credit_posted": 0.0,
        "signed_reserved": 0.0, "signed_posted": 0.0,
        "originals": {}, "invalid_line_ids": [],
    } for source in sources}
    # Normal invoice facts must exist before assigning credit budgets. Cancelled
    # originals retain their state and zero allowance for consistency checks.
    credits = []
    source_ids = set(sources.ids)
    for entry in entries:
        for source in entry.sale_line_ids.filtered(lambda line: line.id in source_ids):
            values = facts[source.id]
            quantity = _invoice_quantity(entry, source)
            if quantity is None:
                if entry.move_id.state != "cancel":
                    values["invalid_line_ids"].append(entry.id)
                continue
            if entry.move_id.move_type == "out_refund":
                if entry.move_id.state != "cancel":
                    credits.append((entry, source, quantity))
                continue
            original = values["originals"].setdefault(entry.move_id.id, _original_facts(entry.move_id))
            if entry.move_id.state != "cancel":
                values["invoice_reserved"] += quantity
                original["invoice_reserved"] += quantity
                if entry.move_id.state == "posted":
                    values["invoice_posted"] += quantity
                    original["invoice_posted"] += quantity
    for entry, source, quantity in credits:
        values = facts[source.id]
        values["credit_reserved"] += quantity
        if entry.move_id.state == "posted":
            values["credit_posted"] += quantity
        original_move = entry.move_id.reversed_entry_id
        if not original_move:
            values["invalid_line_ids"].append(entry.id)
            continue
        original = values["originals"].setdefault(original_move.id, _original_facts(original_move))
        original["credit_reserved"] += quantity
        if entry.move_id.state == "posted":
            original["credit_posted"] += quantity
        if (
            original_move.state != "posted" or original_move.move_type != "out_invoice"
            or original_move.company_id != source.order_id.company_id
            or original["invoice_posted"] <= 0
        ):
            values["invalid_line_ids"].append(entry.id)
    for values in facts.values():
        values["signed_reserved"] = values["invoice_reserved"] - values["credit_reserved"]
        values["signed_posted"] = values["invoice_posted"] - values["credit_posted"]
        values["invalid_line_ids"] = sorted(set(values["invalid_line_ids"]))
    return facts


def lock_financial(lines, extra_companies=None):
    """Company revision, sorted SOs, then bounded account/stock rows.

    ``lines`` includes both current and candidate sources. ``extra_companies``
    carries current/candidate accounting companies, including when there is no
    physical source. Billing authorization belongs to the financial mutation
    caller; this lock itself requires original-actor source reads and company
    authorization, never Sales business write rights. No business SQL writes.
    """
    sources = _authorized_sources(lines)
    entries = _account_lines(sources)
    account_moves = _authorize_accounts(sources, entries)
    stock_moves = sources.env["stock.move"].sudo().search([
        ("sale_line_id", "in", sources.ids),
    ]) if sources else sources.env["stock.move"]
    stock_moves |= stock_moves.origin_returned_move_id
    related_sources = (entries.sale_line_ids | stock_moves.sale_line_id).with_env(sources.env)
    if related_sources:
        _authorized_sources(related_sources)
    orders = (sources | related_sources).order_id
    companies = orders.company_id | account_moves.company_id | entries.company_id
    if stock_moves:
        companies |= companies_for(stock_moves).with_env(sources.env)
    if extra_companies is not None:
        companies |= extra_companies.with_env(sources.env)
    authorize_companies(companies)
    # The revision write is the shared M2a repeatable-read serialization point.
    # All identity authorization above happens before this first technical SQL.
    companies._portfolio_lock_fulfillment()
    pickings = stock_moves.picking_id
    account_lines = account_moves.line_ids
    stock_lines = stock_moves.move_line_ids
    for table, records in (
        ("sale_order", orders), ("sale_order_line", sources | related_sources),
        ("account_move", account_moves), ("account_move_line", account_lines),
        ("stock_picking", pickings), ("stock_move", stock_moves), ("stock_move_line", stock_lines),
    ):
        if records:
            records.env.cr.execute(
                f"SELECT id FROM {table} WHERE id IN %s ORDER BY id FOR UPDATE", [tuple(records.ids)],
            )
            records.invalidate_recordset()
    _authorized_sources(sources | related_sources)
    refreshed = orders.company_id | account_moves.company_id | account_lines.company_id
    if stock_moves:
        refreshed |= companies_for(stock_moves).with_env(sources.env)
    if refreshed - companies:
        raise UserError(_("The financial source company changed concurrently. Please retry."))
