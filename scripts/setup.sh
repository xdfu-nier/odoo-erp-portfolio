#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$PWD/.tools/bin:$PATH"

# All new installations and caches stay inside this project.
export UV_CACHE_DIR="$PWD/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.tools/python"
export XDG_CACHE_HOME="$PWD/.cache"
export PIP_CACHE_DIR="$PWD/.cache/pip"
export PYTHONPYCACHEPREFIX="$PWD/.cache/pycache"
export TMPDIR="$PWD/.runtime/tmp"
mkdir -p "$TMPDIR"

if ! command -v uv >/dev/null 2>&1; then
    echo 'uv is required. Use an existing uv executable or put it in .tools/bin/uv.' >&2
    exit 1
fi

uv python install 3.12 --no-bin
if [ ! -x .venv/bin/python ]; then
    uv venv --python 3.12 .venv
fi
.venv/bin/python scripts/fetch_odoo.py
uv pip install --python .venv/bin/python -r requirements-dev.lock
.venv/bin/python scripts/manage.py configure

echo 'Dependencies installed inside this project. Next: .venv/bin/python scripts/manage.py init'
