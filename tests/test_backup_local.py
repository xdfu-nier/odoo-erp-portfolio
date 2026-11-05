"""Pure CLI refusal and stream/archive contracts; no live PostgreSQL/Odoo."""

import builtins
import contextlib
import io
import json
import os
import tarfile
import unittest
from unittest.mock import patch

from test_release_runtime import ODOO_REVISION, ReleaseFixture, load_script


class BackupLocalTests(ReleaseFixture):
    def setUp(self):
        super().setUp()
        self.backup = load_script("backup_local")
        self.output = self.directory / "new-backup"

    def source_fixture(self):
        project = self.directory / "project"
        upstream = project / ".upstream" / "odoo"
        (upstream / "odoo").mkdir(parents=True)
        (upstream / "odoo-bin").write_bytes(b"#!/usr/bin/env python3\nimport odoo\n")
        (upstream / "odoo" / "__init__.py").write_bytes(b"from . import release\n")
        (upstream / ".portfolio-revision").write_text(ODOO_REVISION + "\n")
        (project / "odoo-source.lock").write_text(ODOO_REVISION + "\n")
        addon = project / "addons" / "portfolio"
        addon.mkdir(parents=True)
        (addon / "__manifest__.py").write_bytes(b"{'version': '18.0.1.0.0'}\n")
        return project, upstream

    def collect_source_provenance(self, project):
        # Git describes this ERP project only. The official upstream has no .git.
        def project_git(*arguments, directory):
            self.assertEqual(directory, project, "must not probe enclosing ERP Git from upstream")
            if arguments == ("rev-parse", "HEAD"):
                return b"a" * 40 + b"\n"
            if arguments == ("status", "--porcelain", "--untracked-files=all", "--", "addons"):
                return b""
            if arguments == ("ls-files", "-z", "--", "addons"):
                return b"addons/portfolio/__manifest__.py\0"
            raise AssertionError("unexpected provenance Git arguments")

        with patch.object(self.backup, "_git", side_effect=project_git), \
                patch("subprocess.run", side_effect=AssertionError("pure source provenance cannot run processes")):
            return self.backup.source_provenance(project_root=project)

    def test_pinned_tarball_without_git_has_actual_source_and_addons_fingerprints(self):
        project, upstream = self.source_fixture()
        self.assertFalse((upstream / ".git").exists())
        result = self.collect_source_provenance(project)
        self.assertEqual(set(result), {"git_commit", "addons_sha256", "upstream_sha256", "odoo_revision"})
        self.assertEqual(result["git_commit"], "a" * 40)
        self.assertEqual(result["odoo_revision"], ODOO_REVISION)
        self.assertRegex(result["addons_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["upstream_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(result["addons_sha256"], result["upstream_sha256"])

    def test_tarball_wrong_or_missing_stamp_or_lock_is_rejected_without_repair(self):
        project, upstream = self.source_fixture()
        for path in (upstream / ".portfolio-revision", project / "odoo-source.lock"):
            before = path.read_bytes()
            for content in (b"0" * 40 + b"\n", b"main\n", b"", b"\xffprivate"):
                path.write_bytes(content)
                self.assert_rejected(lambda: self.collect_source_provenance(project))
                self.assertEqual(path.read_bytes(), content)
            path.unlink()
            self.assert_rejected(lambda: self.collect_source_provenance(project))
            self.assertFalse(path.exists())
            path.write_bytes(before)

    def test_upstream_content_added_removed_or_renamed_changes_compatibility_digest(self):
        project, upstream = self.source_fixture()
        original = self.collect_source_provenance(project)["upstream_sha256"]
        source = upstream / "odoo" / "__init__.py"
        before = source.read_bytes()
        source.write_bytes(before + b"# changed bytes\n")
        self.assertNotEqual(self.collect_source_provenance(project)["upstream_sha256"], original)
        source.write_bytes(before)
        source.rename(source.with_name("renamed.py"))
        self.assertNotEqual(self.collect_source_provenance(project)["upstream_sha256"], original)
        source.with_name("renamed.py").rename(source)
        added = upstream / ".unrecognized-local-file"
        added.write_bytes(b"actual additional source bytes")
        self.assertNotEqual(self.collect_source_provenance(project)["upstream_sha256"], original)
        added.unlink()
        self.assertEqual(self.collect_source_provenance(project)["upstream_sha256"], original)

    def test_upstream_fingerprint_ignores_only_stamp_git_and_generated_python_cache(self):
        project, upstream = self.source_fixture()
        original = self.collect_source_provenance(project)
        (upstream / ".git").mkdir()
        (upstream / ".git" / "index").write_bytes(b"git metadata")
        (upstream / "odoo" / "__pycache__").mkdir()
        (upstream / "odoo" / "__pycache__" / "generated.cpython-312.pyc").write_bytes(b"generated")
        (upstream / "generated.pyc").write_bytes(b"generated")
        for path in upstream.rglob("*"):
            os.utime(path, (1700000000, 1700000000))
        self.assertEqual(self.collect_source_provenance(project), original)
        (upstream / "odoo" / "__init__.py").chmod(0o755)
        self.assertEqual(self.collect_source_provenance(project), original)

    def test_tarball_source_symlinks_and_special_files_are_rejected_without_following(self):
        project, upstream = self.source_fixture()
        link = upstream / "odoo" / "linked.py"
        link.symlink_to(self.config)
        self.assert_rejected(lambda: self.collect_source_provenance(project))
        link.unlink()
        os.mkfifo(upstream / "odoo" / "fifo", 0o600)
        self.assert_rejected(lambda: self.collect_source_provenance(project))

    def test_upstream_source_hash_uses_framed_paths_and_contents(self):
        project, upstream = self.source_fixture()
        source = upstream / "odoo" / "__init__.py"
        source.unlink()
        (upstream / "a").write_bytes(b"bc")
        first = self.collect_source_provenance(project)["upstream_sha256"]
        (upstream / "a").unlink()
        (upstream / "ab").write_bytes(b"c")
        self.assertNotEqual(self.collect_source_provenance(project)["upstream_sha256"], first)

    def invoke(self, *extra, allow=True, database="odoo_portfolio", output=None):
        args = ["--config", str(self.config), "--database", database,
                "--output-dir", str(output or self.output)]
        if allow:
            args.append("--allow-local-release")
        args.extend(extra)
        stdout, stderr = io.StringIO(), io.StringIO()
        # Native imports are forbidden even for a partially valid rejected request.
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"odoo", "psycopg2", "pgserver"}:
                raise AssertionError("native import before target/path validation")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=guarded_import), \
                patch("subprocess.run", side_effect=AssertionError("subprocess before validation")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.backup.main(args)
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertRegex(stderr.getvalue(), r"^\{\"error\": \"[a-z_]+\", \"schema_version\": 1\}\n$")
        self.assertNotIn(self.secret, stderr.getvalue())
        self.assertFalse(self.output.exists())
        return json.loads(stderr.getvalue())["error"]

    def test_missing_opt_in_rejects_before_native_imports_subprocess_or_output_creation(self):
        self.invoke(allow=False)

    def test_any_database_other_than_fixed_source_is_refused_before_io_side_effects(self):
        for database in ("production", "odoo_portfolio_m1_test", "odoo_portfolio_restore_20261105_deadbeef", ""):
            with self.subTest(database=database):
                self.invoke(database=database)

    def test_bad_config_is_refused_without_private_error_body(self):
        self.write_config(db_host="remote.invalid", db_password="secret-in-exception-candidate")
        self.invoke()

    def test_unknown_cli_values_are_never_echoed_by_argument_parser(self):
        self.invoke("--password", self.secret)
        self.invoke("--unexpected=" + self.secret)

    def test_nonruntime_output_and_traversal_are_refused_without_parent_creation(self):
        for path in (self.directory.parent.parent.parent / "release-escape", self.directory / ".." / "new"):
            with self.subTest(path=path):
                self.invoke(output=path)
                self.assertFalse(path.exists())

    def test_existing_directory_file_and_config_output_alias_are_preserved(self):
        existing = self.directory / "existing"
        existing.mkdir()
        marker = existing / "keep"
        marker.write_text("preserve")
        self.invoke(output=existing)
        self.assertEqual(marker.read_text(), "preserve")
        before = self.config.read_bytes()
        self.invoke(output=self.config)
        self.assertEqual(self.config.read_bytes(), before)

    def test_output_symlink_parent_and_hardlinked_config_are_refused(self):
        linked = self.directory / "linked"
        linked.symlink_to(self.data, target_is_directory=True)
        self.invoke(output=linked / "new-backup")
        self.assertEqual(list(self.data.iterdir()), [])
        alias = self.directory / "config-alias"
        os.link(self.config, alias)
        self.invoke()

    def test_filestore_archive_copies_all_regular_files_and_empty_directories_privately(self):
        source = self.directory / "filestore" / "odoo_portfolio"
        (source / "ab").mkdir(parents=True)
        (source / "empty").mkdir()
        (source / "ab" / "ordinary").write_bytes(b"opaque data")
        (source / "checklist").mkdir()
        (source / "checklist" / "gc-marker").write_bytes(b"")
        archive = self.directory / "archive.tar.gz"
        inventory = self.backup.archive_filestore(source, archive)
        self.assertEqual({item["path"] for item in inventory}, {"ab/ordinary", "checklist/gc-marker"})
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive, "r:gz") as handle:
            names = set(handle.getnames())
            self.assertIn("filestore/odoo_portfolio/empty", names)
            self.assertEqual(handle.extractfile("filestore/odoo_portfolio/ab/ordinary").read(), b"opaque data")
            self.assertTrue(all(name.startswith("filestore/odoo_portfolio") for name in names))

    def test_filestore_symlinks_and_special_files_are_rejected_without_reading_them(self):
        source = self.directory / "filestore" / "odoo_portfolio"
        source.mkdir(parents=True)
        link = source / "link"
        link.symlink_to(self.config)
        self.assert_rejected(lambda: self.backup.archive_filestore(source, self.directory / "a.tar.gz"))
        link.unlink()
        os.mkfifo(source / "fifo", 0o600)
        self.assert_rejected(lambda: self.backup.archive_filestore(source, self.directory / "b.tar.gz"))

    def test_archive_never_overwrites_or_reuses_an_existing_output(self):
        source = self.directory / "filestore" / "odoo_portfolio"
        source.mkdir(parents=True)
        archive = self.directory / "archive.tar.gz"
        archive.write_bytes(b"keep-existing-archive")
        self.assert_rejected(lambda: self.backup.archive_filestore(source, archive))
        self.assertEqual(archive.read_bytes(), b"keep-existing-archive")

    def test_public_success_report_excludes_private_snapshot_filelist_and_connection_material(self):
        private_snapshot = {"raw_payload": self.secret, "role_fingerprint": "private-role-hash",
                            "password_hash": "private-password-hash", "attachments": [{"store_fname": "private"}]}
        report = self.backup.safe_backup_report(
            database="odoo_portfolio", provenance={"python_version": "3.12.12", "postgres_version": "16.4",
            "odoo_revision": "98e48692891fc9a3aaa7f53001305b322f53cfa2", "module_versions": {
                "portfolio_trade": "18.0.1.5.0", "portfolio_order_bridge": "18.0.1.3.0"}},
            file_count=2, dump_sha256="a" * 64, archive_sha256="b" * 64,
        )
        self.assertEqual(set(report), {"schema_version", "ok", "database", "versions", "file_count",
                                       "dump_sha256", "archive_sha256", "source_unchanged", "restore_verified"})
        self.assertIs(report["ok"], True)
        self.assertIs(report["source_unchanged"], True)
        self.assertIs(report["restore_verified"], False)
        self.assertEqual(report["file_count"], 2)
        rendered = json.dumps(report)
        for value in (self.secret, *private_snapshot.keys(), "private-role-hash", "private-password-hash"):
            self.assertNotIn(value, rendered)


if __name__ == "__main__":
    unittest.main()
