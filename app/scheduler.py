"""Background scheduler + async SyncManager for sync jobs.

Design goals:
- Sync work NEVER runs inside the request thread or FastAPI lifespan.
- Single global lock ensures at most one sync runs at a time; second trigger
  returns ``already_running`` instead of queueing/blocking.
- Status is exposed via :func:`get_sync_status` for polling.
- ``sync_log`` is written for every known source (copilot/codex/claude/opencode/
  vscode_*/mac_chat_sessions), not just copilot+codex.
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_config
from app.database import get_db
from app.sync.ssh_sync import sync_all_servers, sync_local
from app.tenant import LEGACY_DATA_SCOPE, get_data_scope, tenant_scope

logger = logging.getLogger(__name__)
_SYNC_RUN_SEMAPHORE = threading.BoundedSemaphore(1)

# Sources we track in sync_log when summarising a run.
_KNOWN_SOURCES = (
    "copilot",
    "codex",
    "claude",
    "opencode",
    "vscode_metadata",
    "vscode_memories",
    "vscode_chat_edits",
    "vscode_panel",
    "vscode_debug_logs",
    "mac_chat_sessions",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SyncManager:
    """Thread-safe wrapper around sync_local/sync_all_servers.

    Only one job may run at a time. Callers see immediate ``started`` /
    ``already_running`` responses and poll :py:meth:`status`.
    """

    def __init__(self, scope_id: Optional[str] = None) -> None:
        self._scope_id = scope_id or get_data_scope()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._current_job_id: Optional[str] = None
        self._current_mode: Optional[str] = None
        self._started_at: Optional[str] = None
        self._started_monotonic: Optional[float] = None
        self._last_finished_at: Optional[str] = None
        self._last_job_id: Optional[str] = None
        self._last_mode: Optional[str] = None
        self._last_status: Optional[str] = None  # "success" | "error"
        self._last_result: Any = None
        self._last_error: Optional[str] = None
        self._last_duration_s: Optional[float] = None

    # ------------------------------------------------------------------ public
    def submit(self, mode: str, force: bool = False) -> dict:
        """Submit a sync job. Returns immediately.

        mode: ``local`` | ``all`` | ``force-reimport``
        """
        if mode not in ("local", "all", "force-reimport"):
            raise ValueError(f"unknown sync mode: {mode}")

        with self._lock:
            if self._running:
                return {
                    "status": "already_running",
                    "job_id": self._current_job_id,
                    "mode": self._current_mode,
                    "started_at": self._started_at,
                }
            job_id = uuid.uuid4().hex[:12]
            self._running = True
            self._current_job_id = job_id
            self._current_mode = mode
            self._started_at = _now_iso()
            self._started_monotonic = time.monotonic()
            t = threading.Thread(
                target=self._run_for_scope,
                args=(job_id, mode, force),
                name=f"sync-{job_id}",
                daemon=True,
            )
            self._thread = t
            t.start()
            return {
                "status": "started",
                "job_id": job_id,
                "mode": mode,
                "started_at": self._started_at,
            }

    def _run_for_scope(self, job_id: str, mode: str, force: bool) -> None:
        # ContextVars are not inherited by ordinary threads. Bind the owner
        # explicitly so every query, device, and provider path stays inside
        # the submitting user's physical data directory.
        with _SYNC_RUN_SEMAPHORE, tenant_scope(self._scope_id):
            self._run(job_id, mode, force)

    def status(self) -> dict:
        with self._lock:
            running_for_s = (
                max(0.0, time.monotonic() - self._started_monotonic)
                if self._running and self._started_monotonic is not None
                else 0.0
            )
            configured_limit = max(
                60,
                int(getattr(get_config().sync, "max_run_seconds", 1800) or 1800),
            )
            # A force re-import intentionally scans the full archive and gets a
            # wider window than routine incremental synchronization.
            stale_after_s = (
                configured_limit * 6
                if self._current_mode == "force-reimport"
                else configured_limit
            )
            return {
                "running": self._running,
                "job_id": self._current_job_id,
                "mode": self._current_mode,
                "started_at": self._started_at,
                "last_job_id": self._last_job_id,
                "last_mode": self._last_mode,
                "last_finished_at": self._last_finished_at,
                "last_status": self._last_status,
                "last_result": self._last_result,
                "last_error": self._last_error,
                "last_duration_s": self._last_duration_s,
                "running_for_s": round(running_for_s, 1),
                "stale_after_s": stale_after_s,
                "stale": bool(self._running and running_for_s > stale_after_s),
            }

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until current job (if any) finishes. Returns True if idle."""
        with self._lock:
            t = self._thread
            if not self._running or t is None:
                return True
        t.join(timeout=timeout)
        with self._lock:
            return not self._running

    # ------------------------------------------------------------------ worker
    def _run(self, job_id: str, mode: str, force: bool) -> None:
        started = time.time()
        result: Any = None
        error: Optional[str] = None
        status_str = "success"
        try:
            logger.info(f"[sync {job_id}] start mode={mode} force={force}")
            if mode == "local":
                result = sync_local(force=force)
            elif mode == "all":
                result = sync_all_servers(force=force)
            elif mode == "force-reimport":
                # sync_all_servers already includes the enabled local profile;
                # calling sync_local first imported every local session twice.
                result = sync_all_servers(force=True)
            result_errors = _collect_sync_errors(result)
            if result_errors:
                status_str = "error"
                error = "; ".join(result_errors)[:4000]
            logger.info(f"[sync {job_id}] done in {time.time() - started:.2f}s")
        except Exception as exc:  # noqa: BLE001
            status_str = "error"
            error = f"{type(exc).__name__}: {exc}"
            logger.exception(f"[sync {job_id}] failed: {exc}")
        finally:
            duration = time.time() - started
            finished_at = _now_iso()
            try:
                _write_sync_log(mode, status_str, result, error, finished_at)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[sync {job_id}] sync_log write failed: {exc}")
            with self._lock:
                self._running = False
                self._last_job_id = job_id
                self._last_mode = mode
                self._last_finished_at = finished_at
                self._last_status = status_str
                self._last_result = result
                self._last_error = error
                self._last_duration_s = duration
                self._current_job_id = None
                self._current_mode = None
                self._started_at = None
                self._started_monotonic = None
                self._thread = None


# Per-user managers prevent status/results from one account being observable by
# another. A process-wide semaphore still serializes the expensive sync work.
class TenantSyncManagers:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._managers: dict[str, SyncManager] = {}

    def for_scope(self, scope: Optional[str] = None) -> SyncManager:
        resolved = scope or get_data_scope()
        with self._lock:
            manager = self._managers.get(resolved)
            if manager is None:
                manager = SyncManager(resolved)
                self._managers[resolved] = manager
            return manager

    def submit(self, mode: str, force: bool = False) -> dict:
        return self.for_scope().submit(mode, force)

    def submit_for_scope(
        self, scope: str, mode: str, force: bool = False
    ) -> dict:
        return self.for_scope(scope).submit(mode, force)

    def submit_all_tenants(self, mode: str, force: bool = False) -> dict[str, dict]:
        return {
            scope: self.submit_for_scope(scope, mode, force)
            for scope in _active_data_scopes()
        }

    def status(self) -> dict:
        return self.for_scope().status()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self.for_scope().wait(timeout)

    def aggregate_status(self) -> dict:
        with self._lock:
            managers = list(self._managers.values())
        statuses = [manager.status() for manager in managers]
        stale = [value for value in statuses if value.get("stale")]
        return {
            "running": sum(1 for value in statuses if value.get("running")),
            "stale": bool(stale),
            "stale_count": len(stale),
            "tenant_count": len(statuses),
        }


def _active_data_scopes() -> list[str]:
    if not get_config().auth.enabled:
        return [LEGACY_DATA_SCOPE]
    from app.auth import auth_repository

    return auth_repository.active_data_scopes()


# Singleton tenant-aware manager + APScheduler for periodic runs.
sync_manager = TenantSyncManagers()
scheduler = BackgroundScheduler()


def _iter_source_stats(result: Any):
    """Yield (server_name, source, stats_dict) tuples from a sync result.

    Handles three result shapes:
    - ``sync_local()`` returns ``{source: stats, ...}``
    - ``sync_all_servers()`` returns ``[{server: name, source: stats, ...}, ...]``
    - ``force-reimport`` returns ``{"local": {...}, "remote": [...]}``
    """
    if result is None:
        return
    if isinstance(result, dict) and "local" in result and "remote" in result:
        yield from _iter_source_stats(result["local"])
        yield from _iter_source_stats(result["remote"])
        return
    if isinstance(result, list):
        for entry in result:
            if not isinstance(entry, dict):
                continue
            server = entry.get("server", "unknown")
            if entry.get("skipped"):
                continue
            for source in _KNOWN_SOURCES:
                stats = entry.get(source)
                if isinstance(stats, dict) and stats:
                    yield server, source, stats
            if entry.get("error"):
                yield server, "all", {
                    "errors": 1,
                    "error_message": str(entry["error"]),
                }
        return
    if isinstance(result, dict):
        for source in _KNOWN_SOURCES:
            stats = result.get(source)
            if isinstance(stats, dict):
                yield "local", source, stats


def _collect_sync_errors(result: Any) -> list[str]:
    """Return user-visible device/source errors from a completed sync result."""
    errors: list[str] = []
    if isinstance(result, list):
        for entry in result:
            if not isinstance(entry, dict):
                continue
            server = str(entry.get("server") or "unknown")
            if entry.get("error"):
                errors.append(f"{server}: {entry['error']}")
            for source in _KNOWN_SOURCES:
                stats = entry.get(source)
                if isinstance(stats, dict) and int(stats.get("errors", 0) or 0):
                    errors.append(
                        f"{server}/{source}: {int(stats['errors'])} 个错误"
                    )
        return errors
    if isinstance(result, dict) and "local" in result and "remote" in result:
        return [
            *_collect_sync_errors(result["local"]),
            *_collect_sync_errors(result["remote"]),
        ]
    if isinstance(result, dict):
        if result.get("error"):
            errors.append(str(result["error"]))
        for source in _KNOWN_SOURCES:
            stats = result.get(source)
            if isinstance(stats, dict) and int(stats.get("errors", 0) or 0):
                errors.append(f"local/{source}: {int(stats['errors'])} 个错误")
    return errors


def _write_sync_log(
    mode: str,
    status_str: str,
    result: Any,
    error: Optional[str],
    finished_at: str,
) -> None:
    """Persist per-source rows so the UI can show real progress."""
    with get_db() as conn:
        if status_str == "error" and result is None:
            conn.execute(
                """INSERT INTO sync_log (server_name, source, status,
                       error_message, finished_at)
                   VALUES (?, ?, 'error', ?, ?)""",
                (f"mode:{mode}", "all", error or "unknown error", finished_at),
            )
            return

        wrote_any = False
        for server, source, stats in _iter_source_stats(result):
            synced = int(stats.get("synced", 0) or 0)
            skipped = int(stats.get("skipped", 0) or 0)
            errors = int(stats.get("errors", 0) or 0)
            enriched = int(stats.get("enriched", 0) or 0)
            row_status = "error" if errors else "success"
            error_detail = stats.get("error_message")
            if not error_detail and (skipped or errors):
                error_detail = f"skipped={skipped} errors={errors}"
            conn.execute(
                """INSERT INTO sync_log (server_name, source, status,
                       sessions_synced, messages_synced, error_message,
                       finished_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?)""",
                (
                    server,
                    source,
                    row_status,
                    synced + enriched,
                    error_detail,
                    finished_at,
                ),
            )
            wrote_any = True

        if not wrote_any:
            conn.execute(
                """INSERT INTO sync_log (server_name, source, status,
                       error_message, finished_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (f"mode:{mode}", "all", status_str, error, finished_at),
            )


# ---------------------------------------------------------------- scheduler API
def _scheduled_sync_job() -> None:
    """APScheduler entrypoint — submit through SyncManager (non-blocking)."""
    outcome = sync_manager.submit_all_tenants("all", force=False)
    logger.info(f"Scheduled sync trigger: {outcome}")


def start_scheduler() -> None:
    config = get_config()
    interval = config.sync.interval_minutes
    scheduler.add_job(
        _scheduled_sync_job,
        trigger=IntervalTrigger(minutes=interval),
        id="sync_all",
        replace_existing=True,
        max_instances=1,
    )
    if not scheduler.running:
        scheduler.start()
    logger.info(f"Scheduler started: sync every {interval} minutes")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def trigger_sync_now(mode: str = "all", force: bool = False) -> dict:
    """Submit a sync job through the SyncManager. Non-blocking."""
    return sync_manager.submit(mode, force=force)


def get_sync_status() -> dict:
    return sync_manager.status()


# Backwards-compat: legacy callers may import ``sync_job``.
def sync_job() -> dict:  # pragma: no cover - thin shim
    return sync_manager.submit("all", force=False)
