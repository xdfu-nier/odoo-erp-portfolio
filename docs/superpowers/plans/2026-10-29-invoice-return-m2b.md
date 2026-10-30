# M2b Invoice, Physical Return and Credit Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development and TDD. Fresh implementer per task; independent SPEC then quality review. Root owns all actual Odoo/database/server execution. Freeze source for each RED/GREEN run.

**Goal:** Complete delivery-based customer invoicing and physical-return-before-credit controls for the approved industrial-parts scenario.

**Architecture:** Continue using standard stock moves, account moves/lines and native posting. One company→sorted SO→stock/account lock protocol and protected company revision serializes changes to shared allowances. Authoritative quantities come from completed native physical facts and non-canceled invoice lines, never writable sale counters or caller context. Existing fulfillment flag stays default off until M2c explicitly prepares the local profile.

**Tech Stack:** Fixed Odoo18 `98e48692891fc9a3aaa7f53001305b322f53cfa2`, project-local Python3.12 and PostgreSQL16.2; no new runtime dependency.

## File responsibilities

- `models/financial_policy.py`: private source resolution, original-actor authorization, locks and authoritative quantity calculations.
- `models/account_fulfillment.py`: account.move/account.move.line ORM and native state action boundaries; one savepoint around each public mutation.
- `models/return_fulfillment.py`: genuine customer-return origin, source, company, product, UoM, refund flag and completed-return quantity checks.
- `models/sale_invoicing.py`: protected sale invoice counters/reverse links and finance action using native `_create_invoices`.
- `wizard/partial_credit.py`, its view and ACL: finance-only bounded partial credit from an original posted invoice line.
- `tests/test_fulfillment_ledger.py`, `tests/test_account_fulfillment.py`, `tests/test_return_credit.py`, `tests/test_financial_concurrency.py`: actual native facts, ordinary business actors, live rollback and independent transactions.
- Modify model/wizard/test imports, manifest, security CSV and inherited sales/invoice views only as needed.

## Task 1: Authoritative allowances and shared authorization/locking

- [x] Add real fixtures using `AccountTestInvoicingCommon` and `_create_company(currency_id=CNY)`, which restores CNY after loading the test chart. Separate Sales, Manager, Buyer, Warehouse and Billing users; A/B actual memberships. Shared physical product, no tax, source price100/discount15, native inventory40 and standard SO100/PO60. Do not load a US chart into the existing developer company.
- [x] Write failing tests for private helper contracts before adding production functions. Helpers accept actual sale-line recordsets and return quantities in the sale-line UoM. No UI action or account guard is deployed in this task.

```python
# Concrete fixture progression, through original business actors:
# receive30, deliver70, create native draft invoice70.
self.assertEqual(physical_facts(lines)[line.id], {"delivered": 70.0, "returned": 0.0})
self.assertEqual(financial_facts(lines)[line.id]["invoice_reserved"], 70.0)
self.assertEqual(financial_facts(lines)[line.id]["invoice_posted"], 0.0)
# Native action_post changes posted only; cancel releases reserved.
```

- [x] Root runs only the new ledger class in `odoo_portfolio_m1_test`, explicit worktree addons and port8077; verify missing-helper assertions fail before implementation.
- [x] Implement `physical_facts(lines)`: bounded done moves of each actual product/source, native UoM conversion/precision, customer deliveries minus genuine customer returns. Ignore `qty_delivered` and `qty_delivered_method` caches. Do not let caller `accrual_entry_date` hide later returns; authoritative facts are independent of presentation context.
- [x] Implement `financial_facts(lines)`: genuine linked invoice product lines; state draft/posted reserves, cancel releases; posted counted separately. Convert invoice UoM to source UoM and distinguish normal invoices, credits and original-invoice credit allocation. A controlled physical invoice line has one unambiguous physical sale source; multi-SO invoices remain possible through separate invoice lines. Never repeat a whole merged quantity against multiple sources.
- [x] Implement `lock_financial(lines, extra_companies=None)`: canonical current/candidate source and account-company identities, actual uid company membership/active scope even under native sudo, company revision first, sorted source SO locks, then bounded account/stock locks. Narrow readonly identity inspection and technical revision SQL only; never raw financial or inventory payload SQL.
- [x] Implement `require_billing(env, companies)`: real `account.group_account_invoice` membership under original uid, no env.is_admin/is_system bypass. Source read authorization precedes bounded detail access; no new Sales or company-write grant.
- [x] Freeze. Root runs full existing182 plus ledger tests and helpers; SPEC then fresh quality review, fix/retest and commit.

## Task 2: Native financial and return mutation boundaries

- [x] Add RED tests for invoice before delivery; invoice beyond available quantity; two non-canceled drafts sharing quantity; cancellation release/reset re-reservation; Sales/Buyer/Warehouse cannot invoke native sudo invoice bridge; foreign company/default source; changing quantity/UoM/product/source through direct AML and inverse sale relations; forged sale invoice counters/defaults; direct posted state/create/defaults; late multi-line failure must roll back the whole method.
- [x] Add RED tests for genuine returns: completed customer delivery required, same company/product/sale line, compatible UoM and reverse locations, `to_refund=True`; reject pending/foreign/wrong source, over-return and duplicate return, disabling quantity updates, or changing completed facts. Source location and original done delivery must remain consistent.
- [x] Add RED tests for credit before return, credit beyond returned-uncredited quantity, credit beyond the selected original posted invoice quantity, draft occupation across two original invoices, cancellation release/reset occupation, wrong original product/pricing/source, original invoice withdrawal while an active credit exists, and public full reversal that initially exceeds the available partial allowance.
- [x] Keep original pricing attributable when one original invoice has multiple lines for the same sale source at different prices/discounts/UoMs/taxes. A credit may not reuse the inexpensive line's price for more units than were invoiced on that matching basis. Use exact original-line attribution or a bounded matching pricing group, while retaining the source-global and original-invoice limits.
- [x] Test direct/default derived monetary inputs as well as quantities: raw balance/debit/credit/amount_currency or subtotal values must not create a nonzero physical credit from zero quantity or change the original-price result. Native invoice synchronization and balancing still execute; caller skip/check context is not an authorization grant. Preserve native internally computed tax/counterpart rows within a narrowly bounded execution frame.
- [x] Root verifies genuine RED; distinguish fixture ACL errors from business failures. Positive assertions use original business actors; a failed method is caught using stdlib `unittest.TestCase.assertRaises` inside a fixture-cleanup savepoint, followed by flush/invalidate and before/after facts. Odoo's assertRaises must not supply the rollback being tested.
- [x] Add `account.move` and `account.move.line` guards for current/candidate/default and nested/inverse source identities. Native action_post/reset/cancel uses cursor/uid/exact-record/target-bound private Python capabilities, never context grants. Preserve native balancing, accounts, currencies, dates and tax computation. Protect posted commercial facts and relation clearing; do not break unrelated standard financial adjustments.
- [x] Validate normal invoice allowance as net delivered minus signed non-canceled invoice quantity. Positive credit quantities reserve both global source returned-uncredited quantity and the original posted invoice uncredited quantity; both constraints hold after every create/write/unlink/cancel/reset/post operation under the same locks/savepoint.
- [x] Do not retroactively invalidate a legitimately posted invoice when physical returns precede the credit. Apply normal limits to new/increased claims and posting, retain the original posted invoice, and allow legitimate cancellation/deletion to release claims. A temporary100-invoiced/90-net-delivered state awaiting credit10 is part of the approved sequence, not an error blocking the return.
- [x] Guard physical return completion in the same stock company-revision frame. Derive cumulative completed returns from real moves; no writable numeric return ledger. Retain original native sale/return links and refund flag. Concurrency must not count a half-completed return.
- [x] Protect sale `qty_invoiced`, `qty_to_invoice`, invoice status/derived caches and inverse `invoice_lines` inputs while preserving native compute assignment and native account synchronization. Budgets continue reading real facts, not protected caches alone.
- [x] Override the native SO `_create_invoices` bridge with original Billing/company authorization before any business mutation. Existing Sales wizard ACL does not authorize finance operations; ordinary Billing still need not obtain a Sales role.
- [x] Freeze; root runs full suite, actual role tests and helpers. SPEC then fresh quality review, fix/retest and commit.

## Task 3: Finance actions, bounded partial credit and real races

- [ ] RED tests for actual Billing-only SO invoice action: after first70 it prepares native draft5950, posts through native action; after second30 it creates2550. Sales cannot call the action by RPC. Finance can read the real SO form/action without sale.advance.payment.inv access.
- [ ] RED test for partial-credit wizard from posted first invoice after native return10: create draft10 at original price100/discount15, same product/UoM/taxes/currency/source and `reversed_entry_id`; amount850. Never create a full70/30 credit first and temporarily bypass allowance guards.
- [ ] Implement Billing-only `action_portfolio_create_invoice` using native invoice preparation/creation. Implement a small finance-only TransientModel taking original invoice line and quantity in that line's UoM; display maximum for convenience, revalidate real allowances on the server. Return standard account.move action. ACLs and views expose only role-appropriate controls.
- [ ] RED independent transaction fixtures commit ONLY in `odoo_portfolio_m1_test`. Two invoices compete for one delivered allowance; two credits against different original invoices compete for one returned source allowance; two physical returns compete for one delivered-origin quantity. Second transaction preloads an old snapshot, wait is observed in PostgreSQL, SerializationFailure retries use a new cursor. No mocked race or sleep-only ordering assertion.
- [ ] Implement only missing locking/technical revision behavior demonstrated by RED races. Both serialized orderings must preserve totals. Do not broaden business scope or add a job queue.
- [ ] Execute the complete native role scenario in integration tests: initial40, purchase60; receipt30→delivery70→posted5950; receipt30→delivery30→posted2550; physical return10→postedcredit850. Assert final stock10, gross100/returned10/net90, actual posted journal-line net7650.
- [ ] Freeze; root runs full suite/helpers/Ruff/compile/XML/diff, SPEC then fresh quality review, fix/retest and commit.

## Task 4: Scoped delivery and M2c handoff

- [ ] Keep the existing19 acceptance identities and initial40 inventory evidence untouched. M2b does not auto-enable the policy or initialize developer finance accounts; M2c provides explicit preflighted local preparation and full trace/browser verification.
- [ ] Record exact tested code revision, real test counts/commands, concurrency observations and remaining M2c work. Update README truthfully. No resume employment or impact metrics are fabricated.
- [ ] Create scoped PR referencing #5 without closing the full M2 issue. Require matching-head scaffold/native CI success and downloaded verification artifact with exact test counts before merge; synchronize exact signed main merge and upgrade the project-local developer environment.

Root native command uses the new worktree's absolute addons path, project main config, database `odoo_portfolio_m1_test`, port8077, `--without-demo=all --test-enable --test-tags /portfolio_trade --stop-after-init`; every run writes a unique worktree-local `.runtime/` log. Never print configuration, password files, tokens or cookies.
