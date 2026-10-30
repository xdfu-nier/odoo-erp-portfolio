# M2c Fulfillment Trace and Local Finance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development and TDD. Root owns every actual Odoo/PostgreSQL/server/HTTP execution; implementers write source/tests only and stop at RED/GREEN READY.

**Goal:** Add read-only fulfillment tracing, explicit local CNY accounting preparation, a namespaced native role flow, and automated browser page evidence without changing existing M2b quantity or accounting facts until the operator explicitly enables the local flow.

**Architecture:** A read-only trace service derives from existing native sale/purchase/stock/account records and exposes a form action. A project-local preparation command performs strict preflight and native ORM creation under a namespace; an explicit `--enable-fulfillment` gate is the only path that changes the PARTS-A local policy. A namespaced native role-flow helper executes the approved sales→purchase→receipt/delivery→billing→return/credit sequence once and validates completed records on reruns. HTTP verification reads menus/views and role scopes through real sessions, then drives the existing local Chrome binary through Node's built-in WebSocket client for deterministic page evidence; no external browser dependency is added.

**Tech Stack:** Odoo18 Python ORM/XML, PostgreSQL16.2, project-local Python3.12, JSON-RPC, existing GitHub Actions.

---

### Task 1: Read-only fulfillment trace

**Files:**
- Create: `addons/portfolio_trade/models/fulfillment_trace.py`
- Create: `addons/portfolio_trade/views/fulfillment_trace_views.xml`
- Modify: `addons/portfolio_trade/models/__init__.py`, `addons/portfolio_trade/__manifest__.py`, `addons/portfolio_trade/views/finance_actions_views.xml`
- Test: `addons/portfolio_trade/tests/test_fulfillment_trace.py`, `addons/portfolio_trade/tests/__init__.py`

- [x] Write failing native tests for a confirmed order with PO source, receipt/backorder, delivery/backorder, completed return, two invoices and a partial credit. Assert the trace action/model is missing, then assert the intended fields: source PO IDs, receipt/delivery/return IDs, gross delivered, returned, net delivered, draft/posted invoice quantities and posted signed amount.
- [x] Run the trace class in `odoo_portfolio_m1_test` and record the missing-model RED before production changes.
- [x] Implement a read-only `portfolio.fulfillment.trace` TransientModel. Its required `sale_order_id` is the only input; `default_get`/action preparation calls existing `physical_facts` and `financial_facts`, gathers only same-company linked purchase/picking/move/reversal records, and stores display snapshots for the opened read-only view. Actual source authorization precedes all elevated detail reads.
- [x] Add `sale.order.action_portfolio_trace` with a Billing/Sales/Purchase-readable form action and a `Trace` button whose groups match the existing source permissions. No write/create/unlink ACL beyond the transient model’s native short-lived creation; no financial or stock fields become editable.
- [x] Add tests for Sales, Buyer and Billing visibility, PARTS-B isolation, service-only source (no physical budget), multi-SO invoice line attribution, and action denial before any business mutation. Test XML fields are readonly and the form loads through `get_view`/`web_read`.
- [x] Root runs focused RED, then unfreezes production for minimal implementation, runs full existing337 plus trace tests, helpers/Ruff/compile/XML/diff. Commit only Task1 files after GREEN; fresh SPEC and quality reviews follow. Final a787d561 passes367 native tests (337+30),13 helper tests and both independent reviews; exact evidence in docs/quality/m2c-fulfillment-trace.md.

### Task 2A: Explicit local CNY accounting preparation

**Files:**
- Create: `scripts/prepare_local_finance.py`
- Create: `tests/test_prepare_local_finance.py`
- Create: `addons/portfolio_trade/local_finance.py`
- Create: `addons/portfolio_trade/tests/test_local_finance.py`
- Modify: `addons/portfolio_trade/tests/__init__.py`
- Create: `docs/quality/m2c-finance-preflight.md`

The addon helper is a plain Python module, not a public Odoo model method; installation and upgrades never call it. Its interface is:

```python
def prepare_local_finance(env, *, allow_local=False, check_only=False, enable_fulfillment=False):
    """Preflight/prepare the namespaced CNY profile; caller owns commit."""
```

`check_only=True` reports a missing finance profile as not ready with the six records that would be created, without writes; missing acceptance identities or conflicting records are errors. Combining check-only and enable is invalid. The response contains only safe database/version/company/currency/record IDs/counts and policy state. A complete rerun preserves an already enabled A policy; default preparation never turns a flag on or off. A missing or partial finance profile with an already enabled A policy is an error. Only the actual Settings actor, including the explicit CLI administrator, may prepare accounts; retaining an ordinary UID under sudo is insufficient. New tests use `TransactionCase` with the real acceptance profile and no chart-template loader.

- [ ] Write failing pure Python CLI tests and native `TestLocalFinance` tests for missing preparation API: reject remote host, wrong database, missing acceptance marker, non-CNY PARTS-A, existing conflicting code/name, and duplicate namespace creation; assert no file or database side effect for each failure. Cover the disabled default, explicit enable gate, namespace identity stability, PARTS-B isolation, missing/altered accounts and rollback on an injected late failure.
- [ ] Run project-local unit tests and record RED.
- [ ] Implement the CLI's config/target validation using `acceptance_credentials.validate_target`, then independently verify the actual registry cursor's database and loopback connection through the addon helper. Verify the installed Odoo18 addon provides the M2b finance and M2c trace APIs, report its actual version (release version bumps must remain supported), validate PARTS-A/PARTS-B markers, the original40-unit inventory adjustment, CNY and initial policy flags. Do not use current product quantity as the initial-stock invariant because the approved flow legitimately changes it. Provide `--check` for preflight without mutation.
- [ ] In one database savepoint, use native ORM to find or create only namespace-marked CNY accounts (income, expense, receivable, payable), a sale journal and a CNY pricelist. Refuse same-code/name records without the namespace marker; never load a chart template or overwrite existing account properties. Keep the default policy disabled. Commit only after all postconditions match.
- [ ] Make reruns return the same IDs and counts. Emit a safe JSON summary containing database, namespace, module version, currency, created/reused counts and policy flags; never print credentials or connection strings.
- [ ] Add `--enable-fulfillment` as an explicit, fail-closed operation after finance records and all acceptance identities pass. The flag may enable PARTS-A only; it must refuse a non-loopback target, any PARTS-B change, missing CNY records, or a partially prepared namespace.
- [ ] Root verifies the helper with rollback-isolated native fixtures in `odoo_portfolio_m1_test`, then runs `--check` and explicit preparation against `odoo_portfolio`. Do not persist the acceptance seed in the regression database, where later native tests require an absent profile. Only explicit preparation/enable/flow commands may change the developer database. Verify the original19 identities and initial40 move before and after, PARTS-B unchanged, and add safe evidence to the quality record. Run the full337/helper suite after addon changes.
- [ ] Freeze for root native GREEN and helper checks; commit owned paths only after GREEN, then fresh SPEC and quality review before Task2B starts.

### Task 2B: Gated namespaced native role flow

**Files:**
- Create: `scripts/run_local_fulfillment.py`
- Create: `tests/test_local_fulfillment.py`
- Create: `addons/portfolio_trade/local_fulfillment.py`
- Create: `addons/portfolio_trade/tests/test_local_fulfillment.py`
- Modify: `addons/portfolio_trade/tests/__init__.py`, `docs/quality/m2c-finance-preflight.md`

- [ ] Add native `TestLocalFulfillment` tests for actual Sales/Manager/Buyer/Warehouse/Finance actors, disabled-policy refusal, company isolation, exact final source facts, rerun identity stability, user-edited record conflict and rollback on an injected late failure. Use the real acceptance-data profile and Task2A accounts without a generic chart template.
- [ ] Root runs `/portfolio_trade:TestLocalFulfillment` in `odoo_portfolio_m1_test`, confirms missing-helper RED, and unfreezes minimal implementation.
- [ ] Implement `scripts/run_local_fulfillment.py` and the helper using native ORM methods and the real acceptance users: Sales creates/confirms the approved order, Manager approves it, Buyer creates/confirms the purchase, Warehouse receives/delivers in two batches and completes the return, Finance creates/posts two delivery invoices and an original-line credit. A per-run advisory lock and namespace markers make the transaction idempotent; on failure the savepoint removes every partial business record. A completed run records the source SO, PO, pickings, invoices and credit IDs plus stock10/net90/posted7650.
- [ ] Re-running validates every namespaced record and its native provenance/state/quantity/pricing. It must not create another SO/PO/picking/invoice/credit or overwrite user modifications. Validate the original19 identities and40-unit initial move independently of current stock.
- [ ] Freeze for root focused/full native GREEN and helpers; commit owned paths after GREEN, obtain fresh SPEC then quality review. Root then explicitly enables PARTS-A locally and runs the flow twice, comparing safe record IDs/facts and confirming PARTS-B unchanged.

### Task 3: Real-role HTTP and browser page verification and handoff

**Files:**
- Create: `scripts/verify_m2c_role_flow.py`
- Create: `tests/test_m2c_role_flow.py`
- Create: `scripts/browser_m2c_check.js`
- Modify: `docs/quality/acceptance.md`, `docs/quality/m2b-invoice-return.md`, `README.md`

- [ ] Write failing pure Python tests for role-result schema: seven roles, expected PARTS-A/PARTS-B allowed companies, required menus/models, finance trace/invoice visibility and no Sales write button for finance. Add browser-result schema tests requiring one report per role, successful login, visible menu labels, trace form detection and screenshots stored under the ignored project runtime directory.
- [ ] Run the verifier test and record RED.
- [ ] Implement JSON-RPC session login using existing private credentials without printing them. For each role call `get_view`/`web_read` and menu/action metadata; assert trace form, sales, stock and invoice models according to role. Add a read-only PARTS-B cross-company query that must return no PARTS-A source. The verifier must report HTTP status, role, active/allowed companies, model/action names and policy flags only.
- [ ] Implement `scripts/browser_m2c_check.js` with only Node 22 built-in modules: launch the existing `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` with a project-local temporary profile and CDP port, navigate to the local login/web routes, use WebSocket CDP commands to log in each role through the rendered form, inspect menus and the trace form, and save redacted screenshots/report metadata under `.runtime/m2c-browser/`. Remove the temporary profile after the run; never persist cookies, passwords or config files.
- [ ] Run against the upgraded local service after finance preflight and the explicit local flow are approved; save safe JSON and screenshot reports with role names, allowed companies, HTTP status, menu/model names, policy flags and screenshot paths only. Describe the evidence as automated browser verification, not human UAT.
- [ ] Update README, acceptance matrix and M2c quality record with exact commands, commit, counts, final local facts and remaining M3/M4 scope. Run full337, helpers, Ruff, compile, XML and browser-script syntax checks; perform fresh SPEC and quality reviews for all M2c changes before publishing.

### Final integration gates

- [ ] Merge each reviewed task through the normal branch/CI workflow; never merge an unfinished M2c feature into main.
- [ ] Before claiming M2c complete, verify the local service module version, 19 identities, original40 inventory adjustment, final flow facts stock10/net90/posted7650, seven role logins, PARTS-B isolation, disabled-by-default policy and explicit-enable evidence, plus downloaded matching-head CI artifacts. Keep M3 import idempotency and M4 backup/performance explicitly pending.
