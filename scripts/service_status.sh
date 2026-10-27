#!/bin/bash
set -u

base_url="${ARM_WATCHDOG_URL:-http://127.0.0.1:18080}"
systemctl --user --no-pager --full status agent-record-manager.service || true
echo
echo "Liveness:"
curl --silent --show-error --max-time 3 "$base_url/health/live" || true
echo
echo "Readiness:"
curl --silent --show-error --max-time 3 "$base_url/health/ready" || true
echo
echo "Runtime:"
curl --silent --show-error --max-time 3 "$base_url/api/diagnostics/runtime" || true
echo
echo "Recent logs:"
journalctl --user -u agent-record-manager.service -n 40 --no-pager || true
