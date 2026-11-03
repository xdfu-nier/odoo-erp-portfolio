"""Explicit, real-role fulfillment of the isolated local acceptance bearing.

There is no installation hook or RPC model. Business actions run as the existing
M1 actors; only the namespace markers belong to the invoking Settings actor.
The caller owns the transaction, including its eventual commit.
"""

from odoo import SUPERUSER_ID, Command, _, fields
from odoo.exceptions import AccessError, UserError
from odoo.tools.mail import email_normalize, single_email_re

from .acceptance_data import ROLE_GROUPS, _validate_local_target
from .local_finance import (
    EXPECTED_FINANCE,
    _acceptance_preflight,
    _records_for_namespace,
    prepare_local_finance,
)
from .models.financial_policy import financial_facts, physical_facts

NAMESPACE = "portfolio_trade_fulfillment"
EXPECTED_FULFILLMENT = {
    "sale_order": "sale.order", "purchase_order": "purchase.order",
    "receipt_first": "stock.picking", "receipt_second": "stock.picking",
    "delivery_first": "stock.picking", "delivery_second": "stock.picking",
    "return_picking": "stock.picking", "invoice_first": "account.move",
    "invoice_second": "account.move", "credit_note": "account.move",
}
_LOCK = 762714393
_SENDER_ROLES = ("sales", "manager", "manager2", "buyer", "warehouse", "finance")


def _require(condition, message):
    if not condition:
        raise UserError(_("Local fulfillment profile conflict: %s", message))


def _actor_model(env, acceptance, role, model):
    # with_user deliberately removes caller sudo for every dispatched action.
    return env[model].with_user(acceptance[f"user_{role}"]).with_company(acceptance["company_a"])


def _validate_roles(acceptance):
    for role, group in ROLE_GROUPS.items():
        user = acceptance[f"user_{role}"]
        # has_group can require an uncached native access check. PARTS-B's actor
        # must be checked in B, even when the invoking administrator is in A.
        actor = user.with_user(user).with_context({"allowed_company_ids": user.company_ids.ids})
        _require(actor.has_group(group), "an existing acceptance actor lacks its original required role.")


def _sender_profile(acceptance, *, allow_missing=False):
    senders = {}
    for role in _SENDER_ROLES:
        user = acceptance[f"user_{role}"]
        _require(user.notification_type == "inbox", "acceptance senders require their existing inbox preference.")
        if not user.email:
            _require(allow_missing, "explicitly prepare the six local senders before running fulfillment.")
            email = f"portfolio.acceptance.{role}@portfolio.invalid"
        else:
            # Preserve a valid existing single reserved-domain address verbatim.
            # Unknown nonempty values belong to the user and cannot be replaced.
            normalized = email_normalize(user.email)
            _require(single_email_re.fullmatch(user.email) and normalized
                     and normalized.rsplit("@", 1)[-1].endswith(".invalid"),
                     "a sender has an unknown nonempty address; no address is overwritten.")
            email = user.email
        senders[role] = {"id": user.id, "email": email}
    return senders


def _own(env, key, record):
    env["ir.model.data"].create({
        "module": NAMESPACE, "name": key, "model": record._name,
        "res_id": record.id, "noupdate": True,
    })
    return record


def _bearing_rows(env, model, product):
    # Read globally to reject foreign or unmarked uses of this isolated product.
    return env[model].sudo().with_context(active_test=False).search([("product_id", "=", product.id)])


def _initial_history(env, acceptance):
    product, company = acceptance["product_bearing"], acceptance["company_a"]
    initial, location = acceptance["initial_bearing_move"], acceptance["warehouse_a"].lot_stock_id
    _require(product.with_company(company).qty_available == 40,
             "a new flow requires the original current stock of 40; inventory is never reset.")
    _require(_bearing_rows(env, "stock.move", product) == initial,
             "the bearing already has stock history outside its original inventory evidence.")
    for model in ("sale.order.line", "purchase.order.line", "account.move.line"):
        _require(not _bearing_rows(env, model, product), "the bearing already has unmarked business history.")
    quants = _bearing_rows(env, "stock.quant", product)
    _require(not quants.filtered(lambda quant: quant.reserved_quantity or quant.lot_id or quant.owner_id or quant.package_id),
             "the original bearing inventory has reservations or extra ownership details.")
    internal = quants.filtered(lambda quant: quant.location_id.usage == "internal")
    _require(len(internal) == 1 and internal.location_id == location and internal.quantity == 40,
             "the original inventory is outside the acceptance warehouse.")
    _require(not quants.filtered(lambda quant: quant.location_id not in (location | initial.location_id)),
             "the bearing has unknown inventory locations.")


def _complete_picking(env, acceptance, picking, quantity):
    picking = _actor_model(env, acceptance, "warehouse", "stock.picking").browse(picking.id)
    _require(len(picking.move_ids) == 1, "a native operation has an unexpected number of moves.")
    picking.move_ids.write({"quantity": quantity, "picked": True})
    result = picking.button_validate()
    if isinstance(result, dict):
        _require(result.get("res_model") == "stock.backorder.confirmation",
                 "native validation returned an unsupported wizard.")
        wizard = _actor_model(env, acceptance, "warehouse", "stock.backorder.confirmation").with_context(
            **result.get("context", {}),
        ).create({})
        wizard.process()
    _require(picking.state == "done", "native operation validation did not complete.")
    return _actor_model(env, acceptance, "warehouse", "stock.picking").search([("backorder_id", "=", picking.id)])


def _action_invoice(env, acceptance, action):
    _require(isinstance(action, dict) and action.get("res_model") == "account.move" and action.get("res_id"),
             "the native financial action did not open its created document.")
    move = _actor_model(env, acceptance, "finance", "account.move").browse(action["res_id"]).exists()
    _require(len(move) == 1, "the native financial document is missing.")
    return move


def _execute(env, acceptance, finance):
    company, product = acceptance["company_a"], acceptance["product_bearing"]
    sale = _actor_model(env, acceptance, "sales", "sale.order").create({
        "partner_id": acceptance["customer"].id, "company_id": company.id,
        "user_id": acceptance["user_sales"].id, "warehouse_id": acceptance["warehouse_a"].id,
        "pricelist_id": finance["pricelist_cny"].id,
        "order_line": [Command.create({
            "product_id": product.id, "product_uom_qty": 100, "product_uom": product.uom_id.id,
            "price_unit": 100, "discount": 15, "tax_id": [Command.clear()],
        })],
    })
    _own(env, "sale_order", sale)
    sale.action_portfolio_submit()
    sale.with_user(acceptance["user_manager"]).action_portfolio_approve()
    sale.action_confirm()
    _require(len(sale.picking_ids) == 1, "the sale must create one original native delivery.")
    delivery = _own(env, "delivery_first", sale.picking_ids)
    purchase = _actor_model(env, acceptance, "buyer", "purchase.order").create({
        "partner_id": acceptance["vendor"].id, "company_id": company.id,
        "currency_id": company.currency_id.id, "picking_type_id": acceptance["warehouse_a"].in_type_id.id,
        "portfolio_source_sale_id": sale.id,
        "order_line": [Command.create({
            "product_id": product.id, "product_qty": 60, "product_uom": product.uom_po_id.id,
            "price_unit": 60, "date_planned": fields.Datetime.now(), "taxes_id": [Command.clear()],
        })],
    })
    _own(env, "purchase_order", purchase)
    purchase.button_confirm()
    _require(len(purchase.picking_ids) == 1, "the purchase must create one original native receipt.")
    receipt = _own(env, "receipt_first", purchase.picking_ids)
    receipt_second = _complete_picking(env, acceptance, receipt, 30)
    _require(len(receipt_second) == 1, "the partial receipt must retain its 30-unit native backorder.")
    _own(env, "receipt_second", receipt_second)
    delivery_second = _complete_picking(env, acceptance, delivery, 70)
    _require(len(delivery_second) == 1, "the partial delivery must retain its 30-unit native backorder.")
    _own(env, "delivery_second", delivery_second)
    source = sale.with_user(acceptance["user_finance"])
    first = _action_invoice(env, acceptance, source.action_portfolio_create_invoice())
    _own(env, "invoice_first", first)
    first.action_post()
    _require(not _complete_picking(env, acceptance, receipt_second, 30), "the final receipt left an extra backorder.")
    _require(not _complete_picking(env, acceptance, delivery_second, 30), "the final delivery left an extra backorder.")
    second = _action_invoice(env, acceptance, source.action_portfolio_create_invoice())
    _own(env, "invoice_second", second)
    second.action_post()
    wizard = _actor_model(env, acceptance, "warehouse", "stock.return.picking").with_context(
        active_model="stock.picking", active_id=delivery.id, active_ids=delivery.ids,
    ).create({})
    _require(len(wizard.product_return_moves) == 1, "the native return must select the original bearing move.")
    wizard.product_return_moves.write({"quantity": 10})
    action = wizard.action_create_returns()
    returned = _actor_model(env, acceptance, "warehouse", "stock.picking").browse(action["res_id"])
    _own(env, "return_picking", returned)
    returned.move_ids.write({"to_refund": True})
    _require(not _complete_picking(env, acceptance, returned, 10), "the return left an unexpected backorder.")
    action = first.action_portfolio_partial_credit()
    original_line = first.invoice_line_ids.filtered(lambda entry: entry.display_type == "product")[:1]
    wizard = _actor_model(env, acceptance, "finance", "portfolio.partial.credit").with_context(
        **action.get("context", {}),
    ).create({"original_line_id": original_line.id, "quantity": 10})
    credit = _action_invoice(env, acceptance, wizard.action_create_credit())
    _own(env, "credit_note", credit)
    credit.action_post()


def _validate_sale(acceptance, finance, records):
    company, product = acceptance["company_a"], acceptance["product_bearing"]
    sale, purchase = records["sale_order"], records["purchase_order"]
    sales, manager, buyer = (acceptance[f"user_{role}"] for role in ("sales", "manager", "buyer"))
    _require(
        sale.state == "sale" and sale.create_uid == sales and sale.user_id == sales
        and sale.portfolio_creator_id == sales and sale.portfolio_approval_submitter_id == sales
        and sale.portfolio_approval_state == "approved" and sale.portfolio_approval_current
        and sale.partner_id == acceptance["customer"] and sale.partner_invoice_id == acceptance["customer"]
        and sale.partner_shipping_id == acceptance["customer"] and sale.currency_id == company.currency_id
        and sale.pricelist_id == finance["pricelist_cny"] and sale.warehouse_id == acceptance["warehouse_a"],
        "the sale identity, approval or commercial profile was changed.",
    )
    events = sale.portfolio_approval_history_ids.sorted("id")
    _require(len(events) == 2 and events.mapped("event") == ["submitted", "approved"]
             and events[0].actor_id == sales and events[1].actor_id == manager and sales != manager,
             "the sale does not have its genuine independent approval history.")
    line = sale.order_line
    _require(len(line) == 1 and line.product_id == product and line.product_uom == product.uom_id
             and line.product_uom_qty == 100 and line.price_unit == 100 and line.discount == 15
             and not line.tax_id and not line.display_type and not line.is_downpayment,
             "the source sale line has altered product, quantity, pricing or tax.")
    purchase_line = purchase.order_line
    _require(purchase.state == "purchase" and purchase.create_uid == buyer
             and purchase.partner_id == acceptance["vendor"] and purchase.portfolio_source_sale_id == sale
             and purchase.currency_id == company.currency_id
             and purchase.picking_type_id == acceptance["warehouse_a"].in_type_id
             and len(purchase_line) == 1 and purchase_line.product_id == product
             and purchase_line.product_uom == product.uom_po_id and purchase_line.product_qty == 60
             and purchase_line.price_unit == 60 and not purchase_line.taxes_id
             and not purchase_line.sale_line_id,
             "the purchase identity, source, quantity, pricing or tax was changed.")
    return line, purchase_line


def _validate_stock(acceptance, records, line, purchase_line):
    warehouse, product = acceptance["warehouse_a"], acceptance["product_bearing"]
    expected_moves = acceptance["initial_bearing_move"]
    for key, quantity, creator in (
        ("receipt_first", 30, "buyer"), ("receipt_second", 30, "warehouse"),
        ("delivery_first", 70, "sales"), ("delivery_second", 30, "warehouse"),
        ("return_picking", 10, "warehouse"),
    ):
        picking = records[key]
        move = picking.move_ids
        # Native purchase_stock creates only the original receipt with UID 1;
        # the public purchase confirmation above still runs as the real Buyer.
        creator_id = SUPERUSER_ID if key == "receipt_first" else acceptance[f"user_{creator}"].id
        _require(picking.state == "done" and picking.create_uid.id == creator_id
                 and len(move) == 1 and move.state == "done" and move.product_id == product
                 and move.product_uom == product.uom_id and move.quantity == quantity
                 and move.product_uom_qty == quantity and not move.is_inventory and not move.scrapped
                 and move.picking_type_id.warehouse_id == warehouse,
                 "a completed stock operation has altered identity, quantity or creator.")
        expected_moves |= move
        if key.startswith("receipt"):
            _require(move.purchase_line_id == purchase_line and not move.sale_line_id
                     and picking.picking_type_id == warehouse.in_type_id and picking.partner_id == acceptance["vendor"]
                     and move.location_id.usage == "supplier" and move.location_dest_id == warehouse.lot_stock_id,
                     "a receipt lost its native purchase provenance or warehouse boundary.")
        elif key.startswith("delivery"):
            _require(move.sale_line_id == line and not move.purchase_line_id
                     and picking.picking_type_id == warehouse.out_type_id and picking.partner_id == acceptance["customer"]
                     and move.location_id == warehouse.lot_stock_id and move.location_dest_id.usage == "customer",
                     "a delivery lost its native sales provenance or customer boundary.")
        else:
            _require(move.sale_line_id == line and move.origin_returned_move_id == records["delivery_first"].move_ids
                     and move.to_refund and move.location_id == records["delivery_first"].move_ids.location_dest_id
                     and move.location_dest_id == warehouse.lot_stock_id,
                     "the return no longer reverses the original first delivery into its warehouse.")
        _require(all(detail.state == "done" and detail.product_id == product
                     and detail.company_id == acceptance["company_a"] and detail.picking_id == picking
                     and detail.location_id == move.location_id and detail.location_dest_id == move.location_dest_id
                     and detail.product_uom_id == product.uom_id for detail in move.move_line_ids)
                 and sum(move.move_line_ids.mapped("quantity")) == quantity,
                 "actual stock operation details differ from the marked native operation.")
    _require(records["receipt_second"].backorder_id == records["receipt_first"]
             and records["delivery_second"].backorder_id == records["delivery_first"]
             and not records["receipt_first"].backorder_id and not records["delivery_first"].backorder_id,
             "the two native partial operations lost their backorder provenance.")
    _require(_bearing_rows(line.env, "stock.move", product) == expected_moves,
             "the isolated bearing has extra or missing stock history.")


def _validate_accounts(env, acceptance, finance, records, line):
    entries = env["account.move.line"]
    for key, quantity, amount in (("invoice_first", 70, 5950), ("invoice_second", 30, 2550), ("credit_note", 10, 850)):
        move = records[key]
        credit = key == "credit_note"
        product_line = move.invoice_line_ids.filtered(lambda entry: entry.display_type == "product")
        _require(move.state == "posted" and move.create_uid == acceptance["user_finance"]
                 and move.move_type == ("out_refund" if credit else "out_invoice")
                 and move.partner_id == acceptance["customer"] and move.currency_id == acceptance["company_a"].currency_id
                 and move.journal_id == finance["journal_sale"] and len(product_line) == 1
                 and product_line.sale_line_ids == line and product_line.product_id == line.product_id
                 and product_line.product_uom_id == line.product_uom and product_line.quantity == quantity
                 and product_line.price_unit == 100 and product_line.discount == 15 and not product_line.tax_ids
                 and product_line.account_id == finance["account_income"]
                 and product_line.balance == (amount if credit else -amount)
                 and product_line.amount_currency == product_line.balance
                 and sum(move.line_ids.mapped("balance")) == 0,
                 "a posted financial document has altered source, pricing, product balance or creator.")
        _require(move.reversed_entry_id == (records["invoice_first"] if credit else env["account.move"]),
                 "a credit no longer identifies its original first invoice.")
        entries |= product_line
    _require(_bearing_rows(env, "account.move.line", acceptance["product_bearing"]) == entries,
             "the bearing has unmarked accounting entries.")
    actual = env["account.move.line"].sudo().search([
        ("sale_line_ids", "in", line.ids), ("display_type", "=", "product"),
        ("move_id.state", "=", "posted"), ("move_id.move_type", "in", ["out_invoice", "out_refund"]),
    ])
    _require(actual == entries and -sum(actual.mapped("balance")) == 7650,
             "actual posted product AML balances do not total CNY 7650.")
    return -sum(actual.mapped("balance"))


def _validate_completed(env, acceptance, finance, records):
    company, product = acceptance["company_a"], acceptance["product_bearing"]
    _require(all(record.company_id == company for record in records.values()), "a marked document has a foreign company.")
    line, purchase_line = _validate_sale(acceptance, finance, records)
    _validate_stock(acceptance, records, line, purchase_line)
    _require(_bearing_rows(env, "sale.order.line", product) == line
             and _bearing_rows(env, "purchase.order.line", product) == purchase_line,
             "the isolated bearing has extra sale or purchase history.")
    net_amount = _validate_accounts(env, acceptance, finance, records, line)
    source = line.with_user(acceptance["user_finance"]).with_company(company)
    physical, financial = physical_facts(source)[line.id], financial_facts(source)[line.id]
    _require(physical == {"delivered": 100.0, "returned": 10.0}, "authoritative physical fulfillment facts changed.")
    _require(not financial["invalid_line_ids"] and financial["invoice_reserved"] == 100
             and financial["invoice_posted"] == 100 and financial["credit_reserved"] == 10
             and financial["credit_posted"] == 10 and financial["signed_reserved"] == 90
             and financial["signed_posted"] == 90
             and set(financial["originals"]) == {records["invoice_first"].id, records["invoice_second"].id}
             and financial["originals"][records["invoice_first"].id]["credit_posted"] == 10,
             "authoritative financial source and selected-original facts changed.")
    stock = product.with_company(company).qty_available
    _require(stock == 10, "completed fulfillment requires current stock 10; inventory is never reset.")
    return {
        "database": env.cr.dbname, "namespace": NAMESPACE, "company_id": company.id,
        "identities": {key: {"model": record._name, "id": record.id, "name": record.name}
                       for key, record in sorted(records.items())},
        "actors": {role: acceptance[f"user_{role}"].id for role in ("sales", "manager", "buyer", "warehouse", "finance")},
        "physical": {**physical, "net": physical["delivered"] - physical["returned"], "stock": stock},
        "financial": {key: financial[key] for key in ("invoice_posted", "credit_posted", "signed_posted")}
        | {"net_amount": net_amount, "currency": company.currency_id.name},
    }


def _authorize_local(env, allow_local):
    if allow_local is not True:
        raise UserError(_("Local fulfillment requires explicit local opt-in."))
    _validate_local_target(env)
    actor = env["res.users"].with_user(env.uid).env.user
    if not actor.has_group("base.group_system"):
        raise AccessError(_("Only actual Settings administrators can run local fulfillment."))


def prepare_local_fulfillment_senders(env, *, allow_local=False):
    """Explicitly configure missing reserved A senders; never enable fulfillment."""
    _authorize_local(env, allow_local)
    with env.cr.savepoint():
        ready = prepare_local_finance(env, allow_local=True, check_only=True)
        _require(ready["ready"], "prepare the local finance profile before configuring senders.")
        env.cr.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK])
        acceptance = _acceptance_preflight(env)
        _validate_roles(acceptance)
        # Validate all six before the first native user write. The helper's
        # enclosing savepoint also rolls back a failure in any later write.
        senders = _sender_profile(acceptance, allow_missing=True)
        scoped = env(context={"allowed_company_ids": acceptance["company_a"].ids})
        for role, sender in senders.items():
            user = acceptance[f"user_{role}"].with_env(scoped).with_user(scoped.uid)
            if not user.email:
                user.write({"email": sender["email"]})
        return {
            "database": env.cr.dbname, "company_id": acceptance["company_a"].id,
            "namespace": NAMESPACE, "senders": _sender_profile(acceptance),
        }


def run_local_fulfillment(env, *, allow_local=False):
    """Validate or run the one isolated local flow atomically, without commit."""
    _authorize_local(env, allow_local)
    with env.cr.savepoint():
        # The finance helper is strictly read-only here. Its own preflight checks
        # the installed schema and actual/active A membership before any action.
        ready = prepare_local_finance(env, allow_local=True, check_only=True)
        _require(ready["ready"] and ready["fulfillment_enabled"],
                 "prepare local finance and explicitly enable A before running this flow.")
        env.cr.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK])
        acceptance = _acceptance_preflight(env)
        company = acceptance["company_a"]
        # Drop forged defaults and presentation context, preserving the validated
        # invoking uid. Every business action selects an ordinary actor below.
        scoped = env(context={
            "allowed_company_ids": company.ids, "mail_notify_force_send": False,
        })
        # Keep each metadata record's original uid/elevation, replacing caller
        # stock filters before reading either initial or completed quantities.
        acceptance = {key: record.with_context(scoped.context) for key, record in acceptance.items()}
        _validate_roles(acceptance)
        _sender_profile(acceptance)
        finance = _records_for_namespace(scoped, "portfolio_trade_finance", EXPECTED_FINANCE)
        records = _records_for_namespace(scoped, NAMESPACE, EXPECTED_FULFILLMENT, optional=True)
        if not records:
            _initial_history(scoped, acceptance)
            _execute(scoped, acceptance, finance)
            records = _records_for_namespace(scoped, NAMESPACE, EXPECTED_FULFILLMENT)
        return _validate_completed(scoped, acceptance, finance, records)
