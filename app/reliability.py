"""Low-dependency health checks and runtime observability.

The liveness path deliberately performs no database or thread-pool work.  It
therefore remains responsive when synchronous FastAPI handlers have exhausted
Starlette's shared AnyIO worker pool.  Readiness checks SQLite from a small,
dedicated executor and reports thread-pool saturation separately.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Deque

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import get_config
from app.database import get_db_path


router = APIRouter(tags=["health"])
_STARTED_AT = time.monotonic()
_READY_TIMEOUT_SECONDS = 1.0
_DB_HEALTH_TIMEOUT_MS = 250
_health_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="health-db",
)
_health_future_lock = threading.Lock()
_health_future = None
_health_future_loop = None
_health_cached_result: dict | None = None
_health_cached_until = 0.0


class RuntimeMetrics:
    """Small in-memory metrics registry with no external service dependency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.requests_total = 0
        self.errors_total = 0
        self.duration_seconds_sum = 0.0
        self.slow_requests: Deque[dict] = deque(maxlen=20)

    def begin(self) -> float:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.requests_total += 1
        return time.monotonic()

    def finish(self, started: float, path: str, status_code: int) -> float:
        elapsed = max(0.0, time.monotonic() - started)
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.duration_seconds_sum += elapsed
            if status_code >= 500:
                self.errors_total += 1
            if elapsed >= 1.0:
                self.slow_requests.append({
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": round(elapsed * 1000, 1),
                })
        return elapsed

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "in_flight": self.in_flight,
                "max_in_flight": self.max_in_flight,
                "requests_total": self.requests_total,
                "errors_total": self.errors_total,
                "duration_seconds_sum": round(self.duration_seconds_sum, 6),
                "slow_requests": list(self.slow_requests),
            }


runtime_metrics = RuntimeMetrics()


async def request_metrics_middleware(request: Request, call_next):
    started = runtime_metrics.begin()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        elapsed = runtime_metrics.finish(started, request.url.path, status_code)
        if "response" in locals():
            response.headers["Server-Timing"] = f"app;dur={elapsed * 1000:.1f}"


def _thread_pool_snapshot() -> dict:
    limiter = anyio.to_thread.current_default_thread_limiter()
    total = int(limiter.total_tokens)
    borrowed = int(limiter.borrowed_tokens)
    return {
        "borrowed_tokens": borrowed,
        "total_tokens": total,
        "available_tokens": max(0, total - borrowed),
        "saturated": borrowed >= total,
    }


def _check_database() -> dict:
    started = time.monotonic()
    path = Path(get_db_path()).resolve()
    if not path.exists():
        raise RuntimeError("database file does not exist")
    uri = path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(
        uri,
        uri=True,
        timeout=_DB_HEALTH_TIMEOUT_MS / 1000.0,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={_DB_HEALTH_TIMEOUT_MS}")
        conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
    finally:
        conn.close()
    return {
        "status": "ok",
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
    }


async def _database_readiness() -> dict:
    global _health_future
    global _health_future_loop
    global _health_cached_result
    global _health_cached_until

    now = time.monotonic()
    with _health_future_lock:
        if _health_cached_result is not None and now < _health_cached_until:
            return dict(_health_cached_result)

    loop = asyncio.get_running_loop()
    with _health_future_lock:
        if (
            _health_future is None
            or _health_future_loop is not loop
            or _health_future.done()
        ):
            _health_future = loop.run_in_executor(_health_executor, _check_database)
            _health_future_loop = loop
        future = _health_future
    try:
        # Shield the executor future: timing out one HTTP caller must not cancel
        # the shared check and enqueue another potentially stuck SQLite open on
        # every readiness poll.
        result = await asyncio.wait_for(
            asyncio.shield(future),
            timeout=_READY_TIMEOUT_SECONDS,
        )
        with _health_future_lock:
            _health_cached_result = dict(result)
            _health_cached_until = time.monotonic() + 1.0
        return result
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"database health check exceeded {_READY_TIMEOUT_SECONDS:.1f}s"
        ) from exc


def _background_readiness() -> dict:
    """Inspect in-process background components without blocking on their work."""
    # Lazy imports avoid coupling application import order to worker modules.
    from app.enrichment import enrichment_manager
    from app.scheduler import sync_manager

    agent = enrichment_manager.aggregate_runtime()
    sync = sync_manager.aggregate_status()
    agent_required = bool(agent.get("worker_started"))
    agent_ok = not agent_required or bool(agent.get("worker_alive"))
    sync_ok = not bool(sync.get("stale"))
    return {
        "status": "ok" if agent_ok and sync_ok else "error",
        "agent": {
            "status": "ok" if agent_ok else "dead",
            "worker_alive": bool(agent.get("worker_alive")),
        },
        "sync": {
            "status": "stale" if not sync_ok else "ok",
            "running": bool(sync.get("running")),
        },
    }


def _require_operations_access(request: Request) -> None:
    """Keep process-wide diagnostics out of ordinary tenant accounts."""
    if not get_config().auth.enabled:
        return
    principal = getattr(request.state, "auth_user", None)
    if not principal or not principal.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限。")


@router.get("/health")
@router.get("/health/live")
async def liveness():
    """Event-loop liveness only; safe even if the sync worker pool is full."""
    return {
        "status": "ok",
        "version": "2.0.0",
        "uptime_seconds": round(time.monotonic() - _STARTED_AT, 1),
    }


@router.get("/health/ready")
async def readiness():
    """Return 503 when new business requests should not be sent here."""
    pool = _thread_pool_snapshot()
    try:
        database = await _database_readiness()
    except Exception as exc:  # readiness must return a compact diagnostic
        database = {"status": "error", "detail": str(exc)}
    background = _background_readiness()
    ready = (
        database["status"] == "ok"
        and not pool["saturated"]
        and background["status"] == "ok"
    )
    payload = {
        "status": "ready" if ready else "not_ready",
        "database": database,
        "thread_pool": pool,
        "background": background,
        "in_flight": runtime_metrics.snapshot()["in_flight"],
    }
    return JSONResponse(payload, status_code=200 if ready else 503)


@router.get("/api/diagnostics/runtime")
async def runtime_diagnostics(request: Request):
    """Local operational snapshot used by the watchdog and incident review."""
    _require_operations_access(request)
    db_path = get_db_path()
    sizes = {}
    for suffix, label in (("", "database"), ("-wal", "wal"), ("-shm", "shm")):
        candidate = db_path + suffix
        sizes[label] = os.path.getsize(candidate) if os.path.exists(candidate) else 0
    return {
        "status": "ok",
        "uptime_seconds": round(time.monotonic() - _STARTED_AT, 1),
        "process_threads": threading.active_count(),
        "thread_names": sorted(thread.name for thread in threading.enumerate()),
        "thread_pool": _thread_pool_snapshot(),
        "background": _background_readiness(),
        "http": runtime_metrics.snapshot(),
        "database_bytes": sizes,
    }


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(request: Request):
    """Minimal Prometheus exposition without adding a runtime dependency."""
    _require_operations_access(request)
    metrics = runtime_metrics.snapshot()
    pool = _thread_pool_snapshot()
    lines = [
        "# HELP agent_memory_http_requests_total HTTP requests observed by the process.",
        "# TYPE agent_memory_http_requests_total counter",
        f"agent_memory_http_requests_total {metrics['requests_total']}",
        "# HELP agent_memory_http_errors_total HTTP 5xx responses and uncaught failures.",
        "# TYPE agent_memory_http_errors_total counter",
        f"agent_memory_http_errors_total {metrics['errors_total']}",
        "# HELP agent_memory_http_in_flight Current in-flight HTTP requests.",
        "# TYPE agent_memory_http_in_flight gauge",
        f"agent_memory_http_in_flight {metrics['in_flight']}",
        "# HELP agent_memory_http_duration_seconds_sum Cumulative request duration.",
        "# TYPE agent_memory_http_duration_seconds_sum counter",
        f"agent_memory_http_duration_seconds_sum {metrics['duration_seconds_sum']}",
        "# HELP agent_memory_threadpool_borrowed_tokens Busy AnyIO worker tokens.",
        "# TYPE agent_memory_threadpool_borrowed_tokens gauge",
        f"agent_memory_threadpool_borrowed_tokens {pool['borrowed_tokens']}",
        "# HELP agent_memory_threadpool_total_tokens AnyIO worker capacity.",
        "# TYPE agent_memory_threadpool_total_tokens gauge",
        f"agent_memory_threadpool_total_tokens {pool['total_tokens']}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")
