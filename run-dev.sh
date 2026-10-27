#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
echo "Development server with hot reload: http://0.0.0.0:18080"
exec python3 -m uvicorn app.main:app \
  --host "${ARM_HOST:-0.0.0.0}" \
  --port "${ARM_PORT:-18080}" \
  --reload
