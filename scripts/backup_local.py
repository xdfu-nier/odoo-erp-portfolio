"""Back up the stopped local portfolio source without changing business data."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import socket
import stat
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from release_runtime import (
    DATABASE,
    MAX_EXPANDED,
    MAX_FILES,
    MAX_MANIFEST,
    MODULE_VERSIONS,
    ODOO_REVISION,
    ROOT,
    ReleaseError,
    SafeArgumentParser,
    canonical_json,
    digest,
    exclusive_private,
    file_digest,
    load_local_config,
    pg_environment,
    read_manifest,
    runtime_path,
    safe_relative,
    source_snapshot,
)


def _filestore_entries(source):
    source = runtime_path(source, existing=True)
    if source.name != DATABASE or source.parent.name != "filestore" or not source.is_dir():
        raise ReleaseError("unsafe_path")
    entries, total = [], 0
    try:
        for directory, subdirectories, filenames in os.walk(source, followlinks=False):
            subdirectories.sort()
            filenames.sort()
            for name in (*subdirectories, *filenames):
                path = Path(directory) / name
                info = path.lstat()
                relative = path.relative_to(source).as_posix()
                safe_relative(relative)
                # Include the archive prefix in the bounded tar path length too.
                safe_relative("filestore/" + DATABASE + "/" + relative)
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ReleaseError("unsafe_file")
                if stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1 or info.st_uid != os.getuid():
                        raise ReleaseError("unsafe_file")
                    total += info.st_size
                entries.append((path, info))
                if len(entries) + 1 > MAX_FILES or total > MAX_EXPANDED:
                    raise ReleaseError("archive_failed")
        return source, entries
    except OSError:
        raise ReleaseError("archive_failed") from None


def archive_filestore(source, output):
    """Preserve directories and all ordinary files, including GC/checklist files."""
    source, entries = _filestore_entries(source)
    output = runtime_path(output)
    if output.is_relative_to(source):
        raise ReleaseError("unsafe_path")
    inventory = []
    try:
        with exclusive_private(output) as destination, tarfile.open(fileobj=destination, mode="w:gz") as archive:
            root = tarfile.TarInfo("filestore/" + DATABASE)
            root.type, root.mode = tarfile.DIRTYPE, 0o700
            archive.addfile(root)
            for path, original in entries:
                relative = path.relative_to(source).as_posix()
                member = tarfile.TarInfo("filestore/" + DATABASE + "/" + relative)
                member.mtime = int(original.st_mtime)
                if stat.S_ISDIR(original.st_mode):
                    if path.is_symlink() or not path.is_dir():
                        raise ReleaseError("source_changed")
                    member.type, member.mode = tarfile.DIRTYPE, 0o700
                    archive.addfile(member)
                    continue
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as handle:
                    before = os.fstat(handle.fileno())
                    if (not stat.S_ISREG(before.st_mode) or before.st_ino != original.st_ino
                            or before.st_dev != original.st_dev or before.st_nlink != 1):
                        raise ReleaseError("source_changed")
                    result = hashlib.sha256()
                    while chunk := handle.read(1024 * 1024):
                        result.update(chunk)
                    handle.seek(0)
                    member.size, member.mode = before.st_size, 0o600
                    archive.addfile(member, handle)
                    after = os.fstat(handle.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise ReleaseError("source_changed")
                inventory.append({"path": relative, "size": before.st_size, "sha256": result.hexdigest()})
        # Re-enumeration detects files/directories appearing or disappearing mid-copy.
        _, final_entries = _filestore_entries(source)
        if [(str(p), s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) for p, s in entries] != [
                (str(p), s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) for p, s in final_entries]:
            raise ReleaseError("source_changed")
        return sorted(inventory, key=lambda item: item["path"])
    except (OSError, tarfile.TarError):
        raise ReleaseError("archive_failed") from None


def safe_backup_report(*, database, provenance, file_count, dump_sha256, archive_sha256):
    return {"schema_version": 1, "ok": True, "database": database,
            "versions": {key: provenance[key] for key in (
                "python_version", "postgres_version", "odoo_revision", "module_versions")},
            "file_count": file_count, "dump_sha256": dump_sha256, "archive_sha256": archive_sha256,
            "source_unchanged": True, "restore_verified": False}


def _git(*arguments, directory=ROOT):
    try:
        result = subprocess.run(["git", "-C", str(directory), *arguments], check=True,
                                capture_output=True, timeout=15)
        return result.stdout
    except Exception:
        raise ReleaseError("provenance_failed") from None


def _hash_source_files(source, relative_paths):
    """Frame names and stream actual bytes, independent of clocks and permissions."""
    tree = hashlib.sha256()
    for relative in sorted(relative_paths):
        path = source / relative
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise ReleaseError("provenance_failed")
        for component in (path, *path.parents):
            if component == source:
                break
            if component.is_symlink():
                raise ReleaseError("provenance_failed")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ReleaseError("provenance_failed")
            name = relative.as_posix().encode("utf-8")
            tree.update(len(name).to_bytes(8, "big") + name + before.st_size.to_bytes(8, "big"))
            length = 0
            while chunk := handle.read(1024 * 1024):
                tree.update(chunk)
                length += len(chunk)
            after = os.fstat(handle.fileno())
            if length != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ReleaseError("provenance_failed")
    return tree.hexdigest()


def _upstream_files(source):
    paths = []
    if source.is_symlink() or not source.is_dir():
        raise ReleaseError("provenance_failed")
    for directory, subdirectories, filenames in os.walk(source, followlinks=False):
        for name in (*subdirectories, *filenames):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ReleaseError("provenance_failed")
        subdirectories[:] = sorted(name for name in subdirectories if name not in {".git", "__pycache__"})
        for name in filenames:
            relative = (Path(directory) / name).relative_to(source)
            if (name == ".git" or name.endswith(".pyc") or relative.as_posix() == ".portfolio-revision"):
                continue
            paths.append(relative)
    return sorted(paths)


def source_provenance(project_root=ROOT):
    """Describe fetched tarball bytes; the revision stamp is provenance, not a signature."""
    project = Path(project_root)
    source = project / ".upstream" / "odoo"
    try:
        for path in (project / "odoo-source.lock", source / ".portfolio-revision"):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 128 or path.read_text(encoding="ascii").strip() != ODOO_REVISION:
                raise ReleaseError("provenance_failed")
        if _git("status", "--porcelain", "--untracked-files=all", "--", "addons", directory=project).strip():
            raise ReleaseError("provenance_failed")
        commit = _git("rev-parse", "HEAD", directory=project).decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ReleaseError("provenance_failed")
        addons = [Path(os.fsdecode(raw)) for raw in _git(
            "ls-files", "-z", "--", "addons", directory=project).split(b"\0") if raw]
        if not addons or any(not path.is_relative_to("addons") for path in addons):
            raise ReleaseError("provenance_failed")
        paths = _upstream_files(source)
        if not paths:
            raise ReleaseError("provenance_failed")
        upstream_hash = _hash_source_files(source, paths)
        if _upstream_files(source) != paths:
            raise ReleaseError("provenance_failed")
        return {"git_commit": commit, "addons_sha256": _hash_source_files(project, addons),
                "upstream_sha256": upstream_hash, "odoo_revision": ODOO_REVISION}
    except (OSError, UnicodeError, ValueError):
        raise ReleaseError("provenance_failed") from None


def provenance(config):
    from release_snapshot import connect_readonly

    source = source_provenance()
    connection = connect_readonly(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('server_version')")
            version = cursor.fetchone()[0]
    finally:
        connection.rollback()
        connection.close()
    return {**source,
            "python_version": platform.python_version(), "postgres_version": version,
            "module_versions": MODULE_VERSIONS.copy()}


def require_stopped_source(config):
    from release_snapshot import connect_readonly

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", config.http_port)) == 0:
                raise ReleaseError("source_busy")
        connection = connect_readonly(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()",
                               [DATABASE])
                if cursor.fetchone()[0] != 0:
                    raise ReleaseError("source_busy")
        finally:
            connection.rollback()
            connection.close()
    except OSError:
        raise ReleaseError("source_busy") from None


def dump_database(config, output):
    # Only the wheel bundled inside the existing project venv supplies PG tools.
    from pgserver._commands import POSTGRES_BIN_PATH

    binary = Path(POSTGRES_BIN_PATH) / "pg_dump"
    if not binary.is_absolute() or not binary.resolve().is_relative_to((ROOT / ".venv").resolve()):
        raise ReleaseError("dump_failed")
    try:
        with exclusive_private(output) as handle:
            subprocess.run([str(binary), "--format=custom", "--no-owner", "--no-acl",
                            "--host=127.0.0.1", "--port=" + str(config.db_port),
                            "--username=" + config.db_user, "--dbname=" + DATABASE, "--no-password"],
                           env=pg_environment(config, os.environ), stdout=handle,
                           stderr=subprocess.PIPE, check=True, timeout=120)
    except Exception:
        raise ReleaseError("dump_failed") from None


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-local-release", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.allow_local_release is not True:
            raise ReleaseError("opt_in_required")
        if args.database != DATABASE:
            raise ReleaseError("invalid_target")
        config = load_local_config(args.config)
        output = runtime_path(args.output_dir)
        if output.exists():
            raise ReleaseError("output_exists")
        if not output.parent.is_dir() or output.is_relative_to(config.data_dir):
            raise ReleaseError("unsafe_path")
        source = config.data_dir / "filestore" / DATABASE
        _filestore_entries(source)
        # Every static target, input and output boundary has passed before native I/O.
        require_stopped_source(config)
        before = source_snapshot(config)
        versions = provenance(config)
        output.mkdir(mode=0o700)
        output.chmod(0o700)
        dump_database(config, output / "database.dump")
        files = archive_filestore(source, output / "filestore.tar.gz")
        require_stopped_source(config)
        after = source_snapshot(config)
        if before != after:
            raise ReleaseError("source_changed")
        manifest = {"schema_version": 1, "database": DATABASE,
                    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "provenance": versions, "source_snapshot": before, "source_snapshot_sha256": digest(before),
                    "dump": file_digest(output / "database.dump"),
                    "archive": file_digest(output / "filestore.tar.gz"), "files": files, "restore_verified": False}
        content = canonical_json(manifest)
        if len(content) > MAX_MANIFEST:
            raise ReleaseError("invalid_manifest")
        with exclusive_private(output / "manifest.json") as handle:
            handle.write(content + b"\n")
        read_manifest(output)
        report = safe_backup_report(database=DATABASE, provenance=versions, file_count=len(files),
                                    dump_sha256=manifest["dump"]["sha256"], archive_sha256=manifest["archive"]["sha256"])
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as error:
        code = error.code if isinstance(error, ReleaseError) else "release_failed"
        import sys

        print(json.dumps({"schema_version": 1, "error": code}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
