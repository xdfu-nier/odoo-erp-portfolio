"""Fetch the exact upstream source revision without installing a global package."""

from __future__ import annotations

import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    revision = (ROOT / "odoo-source.lock").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("odoo-source.lock must contain a full Git commit hash")
    target = ROOT / ".upstream" / "odoo"
    stamp = target / ".portfolio-revision"
    if stamp.exists() and stamp.read_text().strip() == revision:
        print(f"Odoo source ready: {revision}")
        return
    if target.exists():
        raise SystemExit("Existing .upstream/odoo has a different or unknown revision; preserving it.")
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    archive = cache / f"odoo-{revision}.tar.gz"
    subprocess.run([
        "curl", "--fail", "--location", "--retry", "3",
        f"https://api.github.com/repos/odoo/odoo/tarball/{revision}",
        "--output", str(archive),
    ], check=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="extract-", dir=target.parent) as temporary:
        staging = Path(temporary)
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(staging, filter="data")
        extracted = list(staging.iterdir())
        if len(extracted) != 1 or not (extracted[0] / "odoo-bin").is_file():
            raise RuntimeError("Unexpected Odoo archive structure")
        extracted[0].rename(target)
    stamp.write_text(revision + "\n")
    print(f"Odoo source ready: {revision}")


if __name__ == "__main__":
    main()
