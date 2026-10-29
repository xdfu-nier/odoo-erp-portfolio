# M2a Procurement Provenance and Stock Integrity Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development and TDD. A fresh implementer owns one task; root owns every Odoo/database/server run and two-stage review. Freeze source before requesting native execution.

**Goal:** Keep native purchase/stock operations while enforcing company-scoped provenance, warehouse completion and nonnegative physical inventory.

**Architecture:** Add an independent company policy and technical revision protocol, provenance-only PO link, then bounded native stock mutation capabilities. Default policy off preserves ordinary companies. Financial/return-credit allowance and full trace follow in M2b/M2c.

**Tech Stack:** Pinned Odoo18, project-local Python3.12/PostgreSQL16.2, existing portfolio_trade module.

## Task 1: Policy infrastructure and procurement provenance

Files: modify `models/res_company.py`, `models/purchase_order.py`, `models/sale_order.py`, company/sale/purchase inherited views, security CSV/rules and manifest; create `tests/test_procurement_provenance.py`.

- [x] Write real TransactionCase fixtures with a controlled company, a second company, separate seller/manager/buyer and AB-authorized buyer, valid M1 stock product/vendor refs and confirmed SO.
- [x] First assertion makes missing feature explicit:

```python
self.assertIn('portfolio_fulfillment_enabled', company._fields)
self.assertIn('portfolio_stock_revision', company._fields)
self.assertIn('portfolio_source_sale_id', self.env['purchase.order']._fields)
```

- [x] Add missing-field RED cases for admin-only policy explicit/context/ir.default values, protected revision defaults/writes, no approval-version bump on fulfillment-only change, same-company confirmed source creation, cross-company/unreadable/draft source rejection, frozen source after submit/confirm, raw and inverse-relation reassignment, and disabled-company compatibility.
- [x] Add positive/negative role tests: buyer can read eligible company source/order lines and create linked PO; cannot edit quote or approve sales; source read scope remains company-bounded. Warehouse/finance retain their native roles. Confirm receipt moves do not inherit MTO sale_line_id merely from the informational header link.
- [x] Root runs `/portfolio_trade:TestProcurementProvenance` against `odoo_portfolio_m1_test`, explicit worktree addons, port8077; verify actual missing-feature failures before implementation.
- [x] Add company Boolean defaultFalse and readonly/copyFalse integer revision default0. Extend existing create/write/default defenses. Fulfillment-only policy changes do not change approval-policy version.
- [x] Implement private `_portfolio_lock_fulfillment`: authorize current actor actual company membership even under sudo, use company lock protocol, update only technical revision for enabled companies, invalidate cache. Never require warehouse company-write ACL or elevate arbitrary payload. Narrow parameterized technical-counter SQL after authorization is allowed; raw inventory/status SQL is forbidden.
- [x] Add header Many2one `portfolio_source_sale_id` with `check_company=True`, `ondelete='restrict'`; validation checks actor read ACL/rule, source state/company and freezes confirmed/submitted source changes. Include current/candidate source and company in one stable company→SO→PO lock order.
- [x] Keep reverse company consistency even when the linked SO is canceled and policies are off. After source-read authorization and sorted row locks, update only the existing SO technical revision so concurrent attachment cannot remain invisible to an old company-transfer snapshot; no SO business-write ACL grant. Observe both real transaction orderings, including all-policy-off custom links.
- [x] Add buyer read-only SO/SOL ACL/rules for eligible controlled-company confirmed orders and existing linked provenance history, without write/create/unlink grants. Other-company and unrelated disabled-company documents remain hidden. Extend company-transfer protection to fulfillment-controlled confirmed records independently of other policy flags.
- [x] Expose the link in standard PO form and read-only related PO navigation in SO, with server-side actions respecting ACL. No route, MTO or planner changes.
- [x] Read actual buyer `get_view` form architecture and call `web_read` with its fields. Keep unsupported sales optional-product and audit widgets restricted to their original reader groups, without adding sales/audit permissions to buyers.
- [x] Freeze source; root runs full existing87 plus new cases. Fix real failures, then independent SPEC review and fresh quality review; commit only after green evidence.

## Task 2: Native stock integrity and actor guards

Files: create focused `models/stock_fulfillment.py`, `tests/test_stock_fulfillment.py`, `tests/test_stock_fulfillment_concurrency.py`; modify model/test imports and manifest/version; use Task1 company helper without changing business scope.

- [x] Build native initial40 inventory adjustment, linked PO60 and approved SO100 at100/15% fixtures with separate warehouse actor. Use Odoo18 quantity/picked fields and native backorder wizard, never assign quant.quantity or done state in positive setup.
- [x] RED tests cover receive30→deliver70/backorder30→receive30→deliver30; insufficient physical stock; own reservation allowance; non-unit compatible UoM; warehouse-only completion; cross-company context spoof under native sudo; public done create/write/defaults; append/edit completed move lines; raw quant quantity/create/identity spoof; legal standard inventory adjustments; disabled-company compatibility.
- [x] Root confirms real failing assertions before implementing guards.
- [x] Verify completion atomicity without Odoo's assertion helper rollback: `TransactionCase.assertRaises` itself creates a savepoint. Use `unittest.TestCase.assertRaises` inside an outer fixture-cleanup savepoint, prepare legitimate draft quantity/picked before the snapshot, then flush/invalidate and compare real facts after the method's error has been consumed. This must test the method's own rollback rather than test-helper cleanup.
- [x] Wrap native stock completion in an execution-local cursor/actor/company-bound capability and savepoint. Verify actor stock membership and actual allowed companies, lock/update technical revision before using quantities, call native super, then verify affected internal source quantities remain nonnegative with native units/precision.
- [x] Guard move/picking/move-line public done promotion and completed business-fact mutations; resolve current and candidate company/location/move identity so a forged related field or transfer cannot opt out. Allow legitimate native split/backorder creations within the bounded private capability.
- [x] Guard raw quant physical values. Private native quantity updaters may issue only their native bounded quant writes under their own internal capability; public inventory_mode/context flags never authorize physical fact changes. Preserve reservations and inventory_quantity adjustments.
- [x] Add fulfillment-enabled confirmed SO commercial immutability independently of price policy, preserving native derived quantity updates. Keep actual warehouse/finance roles from using their native SO write ACL to replace commercial inputs; test composite OwnSales/Stock roles without granting broad SO write rules. For stock safety compare aggregate physical quantity by complete quant identity, not each native quant fragment or free quantity minus this order's reservation.
- [x] Concurrency fixture commits only in the dedicated testDB; two different SOs compete for shared stock in independent cursors, second preloads snapshot, observe Lock wait, retry SerializationFailure with a new cursor, and assert at most available physical stock is completed. No mocked races.
- [x] Freeze; root runs full suite and helper checks, independent SPEC then quality review, fix/retest and commit.

## Task 3: Native CI and delivery handoff

- [ ] Keep final runtime policy default off and current19 acceptance identities unchanged; do not enable an incomplete M2 business policy in a rerun of existing seed. Complete finance/trace readiness in later stages before explicit local activation.
- [ ] Root verifies fresh install/upgrade, all business tests and13 helper tests, Ruff/compile/XML/diff, matching-head native CI and independent reviews.
- [ ] Record actual partial receiving/delivery quantities, commands, revision/commit and known remaining M2b/M2c work. Update README milestone honestly; do not claim the7650 net posted scenario before accounting exists.
- [ ] Publish scoped PR for M2a, reference issue#5 without closing it until M2 is complete. After CI succeeds, merge and synchronize exact commit; keep project-local dependencies and native development workflow.

Native test command pattern (root substitutes only logfile and tag, never credentials):

```bash
.venv/bin/python -X pycache_prefix=.cache/pycache .upstream/odoo/odoo-bin \
  -c /Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.runtime/odoo.conf \
  --addons-path=/Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.upstream/odoo/addons,/Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.worktrees/order-fulfillment/addons \
  -d odoo_portfolio_m1_test -u portfolio_trade --without-demo=all \
  --test-enable --test-tags /portfolio_trade --stop-after-init --http-port=8077 \
  --logfile=/Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.worktrees/order-fulfillment/.runtime/m2a-business-tests.log
```
