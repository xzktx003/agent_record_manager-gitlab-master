#!/usr/bin/env python3
"""Concurrent HTTP smoke/soak check with explicit reliability thresholds."""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from itertools import count


DEFAULT_PATHS = (
    "/health/live",
    "/health/ready",
    "/api/sync/status",
    "/api/work/agent/status?compact=true",
    "/api/work/today?compact=true",
    "/api/sessions/stats",
)


def request_once(base_url: str, path: str, timeout: float) -> dict:
    started = time.monotonic()
    status = 0
    size = 0
    error = ""
    try:
        request = urllib.request.Request(
            base_url.rstrip("/") + path,
            headers={"Accept": "application/json", "Connection": "close"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            size = len(response.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - reported as a load-test failure
        error = f"{type(exc).__name__}: {exc}"
    return {
        "path": path,
        "status": status,
        "bytes": size,
        "duration_ms": (time.monotonic() - started) * 1000,
        "error": error,
    }


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    index = max(0, math.ceil(percentile_value * len(ordered)) - 1)
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--requests", type=int, default=240)
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--interval-ms", type=float, default=100.0)
    parser.add_argument("--p95-ms", type=float, default=1500.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    args = parser.parse_args()

    results: list[dict] = []
    results_lock = threading.Lock()
    sequence = count()
    deadline = time.monotonic() + args.duration if args.duration > 0 else None

    def worker() -> None:
        while True:
            request_number = next(sequence)
            if deadline is None:
                if request_number >= max(1, args.requests):
                    return
            elif time.monotonic() >= deadline:
                return
            path = DEFAULT_PATHS[request_number % len(DEFAULT_PATHS)]
            result = request_once(args.url, path, args.timeout)
            with results_lock:
                results.append(result)
            if args.interval_ms > 0:
                time.sleep(args.interval_ms / 1000.0)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
        futures = [executor.submit(worker) for _ in range(max(1, args.concurrency))]
        for future in futures:
            future.result()

    elapsed = time.monotonic() - started
    failures = [result for result in results if not 200 <= result["status"] < 400]
    durations = [result["duration_ms"] for result in results]
    error_rate = len(failures) / len(results) if results else 1.0
    summary = {
        "requests": len(results),
        "failures": len(failures),
        "error_rate": round(error_rate, 5),
        "elapsed_seconds": round(elapsed, 3),
        "requests_per_second": round(len(results) / elapsed, 2) if elapsed else 0,
        "latency_ms": {
            "min": round(min(durations), 2) if durations else None,
            "p50": round(percentile(durations, 0.50), 2),
            "p95": round(percentile(durations, 0.95), 2),
            "p99": round(percentile(durations, 0.99), 2),
            "max": round(max(durations), 2) if durations else None,
        },
        "thresholds": {
            "p95_ms": args.p95_ms,
            "max_error_rate": args.max_error_rate,
        },
        "sample_failures": failures[:5],
    }
    passed = (
        bool(results)
        and summary["latency_ms"]["p95"] <= args.p95_ms
        and error_rate <= args.max_error_rate
    )
    summary["passed"] = passed
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
