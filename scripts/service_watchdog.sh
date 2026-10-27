#!/bin/bash
set -u

base_url="${ARM_WATCHDOG_URL:-http://127.0.0.1:18080}"
interval="${ARM_WATCHDOG_INTERVAL:-30}"
failure_limit="${ARM_WATCHDOG_FAILURES:-3}"
failures=0
observed_pid=""

while true; do
  sleep "$interval"

  # An inactive unit may have been stopped deliberately.  systemd handles
  # crashes via Restart=on-failure; this watchdog is only for a hung process.
  if ! systemctl --user is-active --quiet agent-record-manager.service; then
    failures=0
    observed_pid=""
    continue
  fi

  current_pid="$(systemctl --user show agent-record-manager.service --property=MainPID --value 2>/dev/null)"
  if [[ -z "$current_pid" || "$current_pid" == "0" ]]; then
    failures=0
    observed_pid=""
    continue
  fi
  if [[ "$current_pid" != "$observed_pid" ]]; then
    if [[ -n "$observed_pid" ]]; then
      echo "monitoring new service pid $current_pid; clearing prior failures"
    fi
    observed_pid="$current_pid"
    failures=0
  fi

  live_ok=0
  ready_ok=0
  curl --fail --silent --show-error --max-time 2 \
    "$base_url/health/live" >/dev/null 2>&1 && live_ok=1
  if [[ "$live_ok" -eq 1 ]]; then
    curl --fail --silent --show-error --max-time 2 \
      "$base_url/health/ready" >/dev/null 2>&1 && ready_ok=1
  fi

  if [[ "$live_ok" -eq 1 && "$ready_ok" -eq 1 ]]; then
    if [[ "$failures" -gt 0 ]]; then
      echo "service recovered after $failures failed checks"
    fi
    failures=0
    continue
  fi

  failures=$((failures + 1))
  echo "health check failed ($failures/$failure_limit, live=$live_ok ready=$ready_ok)"
  if [[ "$failures" -ge "$failure_limit" ]]; then
    echo "restarting agent-record-manager after consecutive health failures"
    systemctl --user restart agent-record-manager.service
    failures=0
    sleep "$interval"
  fi
done
