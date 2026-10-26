# M1 Sales Approval Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development with TDD, then independent specification and code-quality review. Work in `.worktrees/order-approval` on `feat/order-approval`.

**Goal:** Implement REQ-002—004 and the approval part of REQ-012 with actual Odoo/PostgreSQL tests: configurable amount/discount policies, no self approval, immutable history, edit invalidation and concurrency-safe confirmation.

**Architecture:** Extend `res.company`, `sale.order` and `sale.order.line`; add a company-scoped approval audit model. Keep standard sale states unchanged. Every quotation in a company with its approval policy enabled is covered, regardless of UI/API entry point; there is no caller-editable order opt-out flag. All approval-sensitive operations use one parent-order/policy lock protocol and server-side permission checks.

**Tech Stack:** Pinned Odoo 18, Python 3.12, PostgreSQL 16.2, XML views and Odoo `TransactionCase` integration tests.

## Task 1: Write failing integration tests

Create `addons/portfolio_trade/tests/test_sale_approval.py`, import it from the test package and write tests using separate salesperson/manager accounts and two companies. Test externally visible behavior rather than implementation helpers. Intended public methods are `action_portfolio_submit`, `action_portfolio_approve`, and `action_portfolio_reject(reason)`; standard `action_confirm` remains the confirmation entry point.

Example of the central failing expectation, using a managed quotation prepared by a test fixture:

```python
with self.assertRaises(UserError):
    order.with_user(salesperson).action_confirm()
order.with_user(salesperson).action_portfolio_submit()
order.with_user(manager).action_portfolio_approve()
order.with_user(salesperson).action_confirm()
self.assertEqual(order.state, "sale")
```

- [ ] Test amount boundary 49,999.99/50,000 and discount boundary 10/15 percent.
- [ ] Test creator, responsible salesperson and submitter cannot self-approve; normal sales cannot approve or forge state via ORM create/write.
- [ ] Test edits to customer, terms, delivery date and line product/unit/quantity/price/discount/tax invalidate approval; line add/delete and moving a line cannot bypass it.
- [ ] Test immutable company-scoped history, reason required for rejection and resubmission after rejection.
- [ ] Test other-company reads/mutations, standard Sales-menu/API entries in an enabled company, and compatibility for a company with policy disabled. Salespeople cannot disable company policy or opt an individual order out.
- [ ] Run before implementing business models; preserve the real failure output.

## Task 2: Implement policy, audit and state transitions

Create `models/res_company.py`, `models/sale_order.py`, `models/sale_approval.py`, `security/portfolio_security.xml`, `security/ir.model.access.csv` and `views/sale_approval_views.xml`; update imports and manifest. Keep each file focused.

- [ ] Defaults: amount threshold50,000 in company currency, discount limit10%; threshold inputs must be finite and nonnegative, discount within0—100.
- [ ] Company policy enable/configuration is restricted to system settings users, and every order in that company inherits it. Only draft/sent orders can submit or change commercial content; confirmed orders in an enabled company reject those mutations.
- [ ] History records are created only through private, validated business actions; ordinary ACLs expose read access without create/write/unlink. Do not use user-forgeable context flags as a trust boundary.
- [ ] All order and line commercial mutations, submit/approve/reject/confirm and policy changes use a consistent lock order. Re-read state, access rules and snapshot after waiting. Policy changes cannot authorize an order using an outdated approval.
- [ ] Use ORM for business state changes and standard sale confirmation. Raw SQL is limited to explicitly justified locks; no direct sale-state or stock manipulation.
- [ ] Rerun tests until the business cases pass. Do not count absent/concurrency-skipped cases as passed.

## Task 3: Add UI and perform real module upgrade

- [ ] Add submit/approve/reject actions, approval status, read-only history and clear blocking errors to inherited standard sale views.
- [ ] Add company policy configuration only to authorized settings users. Business manager rights must not imply system-administrator rights.
- [ ] Apply the module to a separate integration database with the feature-worktree add-on path; do not modify the verified main development database while tests run.
- [ ] Exercise the approval path using ordinary accounts and verify errors are raised by service methods even without the UI.

## Task 4: Concurrency and review

- [ ] Add independent-transaction tests for approval-versus-edit and confirmation-versus-line add/write/unlink/policy change. Account for PostgreSQL repeatable-read serialization retries explicitly.
- [ ] Run the full custom Odoo suite, configuration tests, Ruff and XML/module upgrade validation.
- [ ] Have an independent reviewer check specification coverage first, then another pass check code quality and authorization bypasses. Fix material findings and rerun affected tests.
- [ ] Update actual UAT coverage and implementation status, commit the feature, and create a reviewable GitHub PR linked to the approved baseline.

REQ-001 complete catalog enforcement, purchasing, inventory, invoicing and external import are separate tasks; do not claim the entire M1/M2/M3 milestone passed if those requirements remain unfinished.
