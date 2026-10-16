#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

PYTHON_BIN=${PYTHON_BIN:-python3}
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python 3.11 or newer is required on the AMP host." >&2
    exit 1
fi

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Python 3.11 or newer is required")'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --upgrade pip

.venv/bin/python -m pip install -e .

echo "QuietLeaf is installed. Configure .env, then start it with: sh amp-start.sh"
