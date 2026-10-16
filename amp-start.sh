#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

if [ -f "$ROOT/.env" ]; then
    set -a
    . "$ROOT/.env"
    set +a
fi

if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "QuietLeaf is not installed. Run: sh install-amp.sh" >&2
    exit 1
fi

if [ -z "${QUIETLEAF_API_TOKEN:-}" ]; then
    echo "Set QUIETLEAF_API_TOKEN in $ROOT/.env before starting QuietLeaf." >&2
    exit 1
fi

PORT_VALUE=${1:-${PORT:-8765}}
case "$PORT_VALUE" in
    *[!0-9]*|'') echo "The service port must be a number." >&2; exit 1 ;;
esac
if [ "$PORT_VALUE" -lt 1 ] || [ "$PORT_VALUE" -gt 65535 ]; then
    echo "The service port must be between 1 and 65535." >&2
    exit 1
fi

export QUIETLEAF_CATALOG_DIR=${QUIETLEAF_CATALOG_DIR:-$ROOT/catalog}
export QUIETLEAF_ARCHIVE_DIR=${QUIETLEAF_ARCHIVE_DIR:-$ROOT/archive}

exec "$ROOT/.venv/bin/python" -m uvicorn quietleaf_service.app:app \
    --host "${QUIETLEAF_BIND_HOST:-127.0.0.1}" \
    --port "$PORT_VALUE" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips "${QUIETLEAF_FORWARDED_ALLOW_IPS:-127.0.0.1}"
