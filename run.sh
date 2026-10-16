#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
exec python -m uvicorn quietleaf_service.app:app --host 127.0.0.1 --port "${PORT:-8765}"
