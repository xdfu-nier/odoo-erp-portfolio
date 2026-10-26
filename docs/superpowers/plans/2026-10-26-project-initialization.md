# Project initialization implementation plan

**Goal:** Create and publish the project skeleton requested by the user, with project-local development tooling.

**Architecture:** Keep upstream Odoo separate from the custom add-on. Store downloaded interpreters, the virtual environment, PostgreSQL binaries, data and cache inside the repository directory and exclude them from Git.

**Tech Stack:** Odoo 18 Community, Python 3.12, PostgreSQL 16 provided by the pgserver wheel, VS Code, GitHub Actions.

- [x] Create the add-on manifest, application navigation and editor configuration.
- [x] Write and run failing tests for project-local runtime paths, configuration validation and preserving user configuration.
- [x] Implement local configuration helpers, setup and database lifecycle commands.
- [x] Validate the skeleton, document actual implementation status and commit only source/configuration examples.
- [x] Create `xdfu-nier/odoo-erp-portfolio` as a public GitHub repository and push the initial commit.

Business features and public customer-case analysis remain the next phase after initialization. Do not describe them as implemented in this milestone.
