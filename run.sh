#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
echo "=== Agent Memory Manager ==="
echo "Production server: http://0.0.0.0:18080"
exec python3 -m uvicorn app.main:app \
  --host "${ARM_HOST:-0.0.0.0}" \
  --port "${ARM_PORT:-18080}" \
  --limit-concurrency "${ARM_LIMIT_CONCURRENCY:-100}" \
  --timeout-keep-alive "${ARM_KEEP_ALIVE_SECONDS:-5}" \
  --log-level "${ARM_LOG_LEVEL:-info}"
