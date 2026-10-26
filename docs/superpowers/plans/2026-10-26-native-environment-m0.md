# M0 Native Odoo Environment Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan in the current session. Keep business approval work separate until M0 passes.

**Goal:** Actually install and verify a project-local Odoo 18 / Python 3.12 / PostgreSQL development environment, fulfilling REQ-013.

**Architecture:** Use the existing `.tools`, `.venv`, `.upstream` and `.runtime` layout. Reuse existing lifecycle commands, reproduce failures before fixing them, and preserve generated local credentials and data.

**Tech Stack:** Python 3.12.13, pinned Odoo 18 source, pgserver PostgreSQL binaries, uv and HTTP JSON-RPC.

## Task 1: Verify interpreter and project dependencies

- [x] Verify the downloaded interpreter against the official release asset SHA256 and extract under `.tools/python/cpython-3.12.13-macos-aarch64-none`.
- [x] Create `.venv` with that exact interpreter.
- [x] Install `requirements-dev.txt` with absolute project-local `UV_CACHE_DIR`, `TMPDIR`, `PIP_CACHE_DIR` and `PYTHONPYCACHEPREFIX`.
- [x] Run `.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests -v`; expect the four existing configuration tests to pass.
- [x] Verify `.venv/bin/python -c 'import odoo'` with `PYTHONPATH=.upstream/odoo`; preserve the exact error before any dependency adjustment.

## Task 2: Initialize PostgreSQL and install the module

- [x] Run `.venv/bin/python scripts/manage.py init`; unified command session recorded exit0 and66 loaded modules. PostgreSQL logs are in `.runtime/postgres.log`.
- [x] If initialization fails, identify whether the error occurs in bundled binary loading, database permissions or Odoo startup. Write a regression test for any script behavior that needs to change before implementing the fix.
- [x] Query PostgreSQL to verify the `odoo` role has LOGIN and CREATEDB but not SUPERUSER or CREATEROLE; query `ir_module_module` for installed `portfolio_trade` and standard dependencies.
- [x] Record actual Python, PostgreSQL, Odoo revision and module versions in `docs/quality/m0-verification.md`.

## Task 3: Verify web startup and login

- [x] Run `.venv/bin/python scripts/manage.py start` in a managed session and wait for the module-loading log.
- [x] Verify `GET /web/login` returns 200.
- [x] Use a cookie-preserving HTTP client to call `/web/session/authenticate` with the local development database and initial admin account; assert a real user ID is returned without publishing passwords or cookies.
- [x] With the session, read the installed module and its menu actions; validate the referenced standard models exist.
- [x] Keep the verified development server available at127.0.0.1:8069 and document the separate project database lifecycle.

## Task 4: Publish verifiable status

- [x] Update README and milestone state only with checks actually run. Keep all business UAT cases unexecuted.
- [x] Run configuration tests, `bash -n scripts/setup.sh`, Python compilation and `git diff --check`.
- [ ] Review the exact changed files, commit M0 separately from the approved requirement baseline, and publish through a pull request.

No business-feature implementation or acceptance claim is included in this environment milestone.
