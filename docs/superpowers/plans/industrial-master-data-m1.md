# M1 Industrial Master Data Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development and TDD. Implement in this isolated master-data worktree; parent owns all Odoo database executions.

**Goal:** Complete the master-data and role preparation part of M1 with real service-entry validation, repeatable acceptance fixtures and actual Odoo regression evidence.

**Architecture:** Add a system-admin-controlled company master-data policy independent of price approval. Reuse standard partner/product/UoM/supplier fields, validate at sale submit/confirm and purchase confirmation, then supply explicit local-only acceptance data without auto-installing it.

**Tech Stack:** Pinned Odoo18, project-local Python3.12/PostgreSQL16.2, existing `portfolio_trade` extension.

## Task 1: Tests first

- [x] Add `addons/portfolio_trade/tests/test_master_data.py`, imported by the test package.
- [x] Write failing cases for missing/ambiguous customer/vendor ref or SKU, wrong company, inactive data, incompatible UoM, stock-product invoice policy and supplier requirements; add a complete valid quote/purchase and policy-disabled compatibility.
- [x] Include role checks (sales cannot configure policy, approving manager is not system administrator) and price-policy-off/master-policy-on independence.
- [x] Notify parent to run the focused suite against the dedicated test database and record red output before implementation.

## Task 2: Business-entry validation

- [x] Add focused `models/master_data.py` helpers and company setting/view; use standard ORM and explicit company scopes, never global mutation sudo.
- [x] Integrate with `action_portfolio_submit`, standard sales `action_confirm` and purchase `button_confirm` so no menu/API route bypasses validation.
- [x] Preserve ordinary standard-company creation and partial draft entry; report actionable errors at business gates rather than changing all framework fields to required.
- [x] Freeze source and ask parent to run full module upgrade/tests. Fix only real failures and preserve original approval coverage.

## Task 3: Acceptance-data preparation

- [x] Add explicit developer CLI/tool and `tests/test_acceptance_data.py` or Odoo integration tests for idempotence, no overwrite, role/company boundaries and credential handling.
- [x] Create fake companies, proper standard business roles, counterparties, three product types, valid supplier information and a standard initial inventory adjustment of40 bearings exactly once.
- [x] Store generated account passwords only in a mode0600 project-runtime file; suppress invite emails. Do not commit credentials or claim seed data is customer data.
- [x] Parent runs actual tool twice and verifies record counts, company ownership and inventory movement history; do not call the tool complete merely because source exists.

## Task 4: Review and publish

- [x] Run original approval + new catalog + fixture tests in the real Odoo/PostgreSQL environment; parent provides exclusive test execution.
- [x] Run helper unit tests, Ruff, XML loading and diff checks; independently review spec compliance, then quality/security.
- [x] Record actual UAT-001/002 evidence and remaining M2/M3/M4 work, create PR linked toGitHub issue#3, and wait for matching-head nativeCI before merge.

Do not implement purchasing fulfillment, invoice posting or external-source mapping in this milestone. Their existing roadmap tasks remain open.

Engineering checklist completed through merged PR #9; full browser UAT remains unexecuted.
