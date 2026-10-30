# M2c Fulfillment Trace and Local Finance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development and TDD. Root owns every actual Odoo/PostgreSQL/server/HTTP execution; implementers write source/tests only and stop at RED/GREEN READY.

**Goal:** Add read-only fulfillment tracing, idempotent local CNY accounting preparation, and real-role page verification without changing existing M2b quantity or accounting facts.

**Architecture:** A read-only trace service derives from existing native sale/purchase/stock/account records and exposes a form action. A project-local preparation command performs strict preflight and native ORM creation under a namespace. HTTP verification reads menus/views and role scopes through real sessions; it never enables fulfillment policy automatically.

**Tech Stack:** Odoo18 Python ORM/XML, PostgreSQL16.2, project-local Python3.12, JSON-RPC, existing GitHub Actions.

---

### Task 1: Read-only fulfillment trace

**Files:**
- Create: `addons/portfolio_trade/models/fulfillment_trace.py`
- Create: `addons/portfolio_trade/views/fulfillment_trace_views.xml`
- Modify: `addons/portfolio_trade/models/__init__.py`, `addons/portfolio_trade/__manifest__.py`, `addons/portfolio_trade/views/finance_actions_views.xml`
- Test: `addons/portfolio_trade/tests/test_fulfillment_trace.py`, `addons/portfolio_trade/tests/__init__.py`

- [ ] Write failing native tests for a confirmed order with PO source, receipt/backorder, delivery/backorder, completed return, two invoices and a partial credit. Assert the trace action/model is missing, then assert the intended fields: source PO IDs, receipt/delivery/return IDs, gross delivered, returned, net delivered, draft/posted invoice quantities and posted signed amount.
- [ ] Run the trace class in `odoo_portfolio_m1_test` and record the missing-model RED before production changes.
- [ ] Implement a read-only `portfolio.fulfillment.trace` TransientModel. Its required `sale_order_id` is the only input; `default_get`/action preparation calls existing `physical_facts` and `financial_facts`, gathers only same-company linked purchase/picking/move/reversal records, and stores display snapshots for the opened read-only view. Actual source authorization precedes all elevated detail reads.
- [ ] Add `sale.order.action_portfolio_trace` with a Billing/Sales/Purchase-readable form action and a `Trace` button whose groups match the existing source permissions. No write/create/unlink ACL beyond the transient model’s native short-lived creation; no financial or stock fields become editable.
- [ ] Add tests for Sales, Buyer and Billing visibility, PARTS-B isolation, service-only source (no physical budget), multi-SO invoice line attribution, and action denial before any business mutation. Test XML fields are readonly and the form loads through `get_view`/`web_read`.
- [ ] Root runs focused RED, then unfreezes production for minimal implementation, runs full existing337 plus trace tests, helpers/Ruff/compile/XML/diff. Commit only Task1 files after GREEN; fresh SPEC and quality reviews follow.

### Task 2: Explicit local CNY accounting preparation

**Files:**
- Create: `scripts/prepare_local_finance.py`
- Create: `scripts/test_prepare_local_finance.py`
- Create: `docs/quality/m2c-finance-preflight.md`

- [ ] Write failing Python tests for missing command/preflight behavior: reject remote host, wrong database, missing acceptance marker, non-CNY PARTS-A, existing conflicting code/name, and duplicate namespace creation; assert no file or database side effect for each failure.
- [ ] Run project-local unit tests and record RED.
- [ ] Implement strict local target validation using the existing `Settings` parser and loopback PostgreSQL connection. Through an Odoo registry cursor, verify module version1.4.0, PARTS-A/PARTS-B markers, 40-unit inventory, CNY, and fulfillment flags off before any create.
- [ ] In one database savepoint, use native ORM to find or create only namespace-marked CNY accounts (income, expense, receivable, payable), a sale journal and a CNY pricelist. Refuse same-code/name records without the namespace marker; never load a chart template or overwrite existing account properties. Commit only after all postconditions match.
- [ ] Make reruns return the same IDs and counts. Emit a safe JSON summary containing database, namespace, module version, currency, created/reused counts and policy flags; never print credentials or connection strings.
- [ ] Root runs the script against a disposable isolated finance-preflight database first, then a read-only preflight against `odoo_portfolio`; do not enable fulfillment or overwrite the existing 19 identities/40 stock. Add actual evidence to the quality record and run the full337/helper suite after any addon changes.

### Task 3: Real-role HTTP page verification and handoff

**Files:**
- Create: `scripts/verify_m2c_role_flow.py`
- Create: `tests/test_m2c_role_flow.py`
- Modify: `docs/quality/acceptance.md`, `docs/quality/m2b-invoice-return.md`, `README.md`

- [ ] Write failing pure Python tests for role-result schema: seven roles, expected PARTS-A/PARTS-B allowed companies, required menus/models, finance trace/invoice visibility and no Sales write button for finance.
- [ ] Run the verifier test and record RED.
- [ ] Implement JSON-RPC session login using existing private credentials without printing them. For each role call `get_view`/`web_read` and menu/action metadata; assert trace form, sales, stock and invoice models according to role. Add a read-only PARTS-B cross-company query that must return no PARTS-A source.
- [ ] Run against the upgraded local service after finance preflight is explicitly approved; save a safe JSON report with role names, allowed companies, HTTP status, menu/model names and policy flags only. Do not claim human browser screenshots.
- [ ] Update README, acceptance matrix and M2c quality record with exact command, commit, counts and remaining M3/M4 scope. Run full337, helpers, Ruff, compile and XML checks; perform fresh SPEC and quality reviews for all M2c changes before publishing.

### Final integration gates

- [ ] Merge each reviewed task through the normal branch/CI workflow; never merge an unfinished M2c feature into main.
- [ ] Before claiming M2c complete, verify the local service module version, 19 identities, 40 inventory, seven role logins, policy flags off by default, and downloaded matching-head CI artifacts. Keep M3 import idempotency and M4 backup/performance explicitly pending.
