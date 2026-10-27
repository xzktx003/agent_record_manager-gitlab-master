"""Background session-to-work-item enrichment using the configured Agent."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.agent_provider import (
    SESSION_ANALYSIS_PROMPT_VERSION,
    AgentProviderError,
    analyze_session_segment,
    get_agent_provider,
    provider_status,
)
from app.config import get_config
from app.database import get_db
from app.tenant import get_data_scope, tenant_scope
from app.work_memory import (
    StaleSessionAnalysisError,
    apply_session_analyses,
    compute_session_hash,
    enqueue_backfill,
    get_enrichment_status,
    mark_session_pending,
)


logger = logging.getLogger(__name__)

_PROVIDER_STATUS_TTL_SECONDS = 300.0
_WORKER_DB_BUSY_TIMEOUT_MS = 180_000

_ANALYZABLE_ROLES = ("user", "assistant")
_SKIP_MESSAGE_TYPES = {
    "reasoning", "tool_call", "function_call", "function_call_output",
    "execution_complete", "tool_output", "exec_command", "mcp_tool_call",
    "patch_apply", "web_search", "subagent_start",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_session_context(
    session_id: str,
    processed_message_count: int = 0,
    retry_level: int = 0,
) -> tuple[dict, list[str], str, int]:
    """Load a bounded, chronologically representative conversation sample."""
    config = get_config().agent
    with get_db(busy_timeout_ms=_WORKER_DB_BUSY_TIMEOUT_MS) as conn:
        session = conn.execute(
            """SELECT id,source,server_name,title,cwd,model,start_time,end_time,
                      message_count FROM sessions WHERE id=?""",
            (session_id,),
        ).fetchone()
        if not session:
            raise ValueError(f"会话不存在: {session_id}")
        source_hash = compute_session_hash(conn, session_id)
        count = int(conn.execute(
            f"""SELECT COUNT(*) FROM messages WHERE session_id=?
                AND role IN ({','.join('?' for _ in _ANALYZABLE_ROLES)})
                AND COALESCE(msg_type,'') NOT IN ({','.join('?' for _ in _SKIP_MESSAGE_TYPES)})
                AND length(trim(COALESCE(content,''))) > 0""",
            (session_id, *_ANALYZABLE_ROLES, *_SKIP_MESSAGE_TYPES),
        ).fetchone()[0])
        processed_message_count = max(0, min(int(processed_message_count or 0), count))
        context_before = min(4, processed_message_count)
        first_row = max(1, processed_message_count - context_before + 1)
        incremental = processed_message_count > 0
        max_messages = max(120, config.max_segments_per_session * 90)
        rows = conn.execute(
            f"""WITH ordered AS (
                    SELECT id,role,timestamp,msg_type,
                           substr(content,1,16000) AS content,
                           ROW_NUMBER() OVER (ORDER BY id) AS rn,
                           COUNT(*) OVER () AS total
                    FROM messages WHERE session_id=?
                      AND role IN ({','.join('?' for _ in _ANALYZABLE_ROLES)})
                      AND COALESCE(msg_type,'') NOT IN ({','.join('?' for _ in _SKIP_MESSAGE_TYPES)})
                      AND length(trim(COALESCE(content,''))) > 0
                )
                SELECT id,role,timestamp,msg_type,content,rn FROM ordered
                WHERE rn >= ? AND rn < ?
                ORDER BY id""",
            (
                session_id, *_ANALYZABLE_ROLES, *_SKIP_MESSAGE_TYPES,
                first_row, first_row + max_messages,
            ),
        ).fetchall()
        correction_examples = []
        for feedback in conn.execute(
            """SELECT action,before_json,after_json FROM work_feedback
               WHERE action IN ('edit','approve') AND before_json IS NOT NULL
                 AND after_json IS NOT NULL ORDER BY id DESC LIMIT 20"""
        ).fetchall():
            try:
                before = json.loads(feedback["before_json"])
                after = json.loads(feedback["after_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            changes = {}
            for key in (
                "title", "kind", "project_name", "category", "status",
                "priority", "summary", "problem", "root_cause", "solution",
                "outcome", "major_task_id",
            ):
                if before.get(key) != after.get(key):
                    changes[key] = {
                        "before": str(before.get(key) or "")[:500],
                        "after": str(after.get(key) or "")[:500],
                    }
            if changes:
                correction_examples.append(changes)

    rendered: list[tuple[int, str]] = []
    for row in rows:
        content = (row["content"] or "").strip()
        # Codex/Claude context injection records are useful only in tiny doses;
        # they otherwise drown out the actual user task.
        if row["msg_type"] == "input_text" and len(content) > 4000:
            content = content[:4000] + "\n…[context truncated]"
        if row["rn"] <= processed_message_count and len(content) > 1000:
            content = content[:1000] + "\n…[prior context truncated]"
        rendered.append((
            int(row["rn"]),
            f"[message_id={row['id']} role={row['role']} time={row['timestamp'] or ''}]\n{content}",
        ))

    base_max_chars = max(4000, int(config.max_input_chars or 12000))
    retry_level = max(0, int(retry_level or 0))
    max_chars = max(4000, base_max_chars // (2 ** retry_level))
    segments: list[str] = []
    segment_last_rows: list[int] = []
    current: list[str] = []
    current_size = 0
    current_last_row = processed_message_count
    for row_number, message in rendered:
        message = message[:max_chars]
        if current and current_size + len(message) + 2 > max_chars:
            segments.append("\n\n".join(current))
            segment_last_rows.append(current_last_row)
            current, current_size = [], 0
        current.append(message)
        current_size += len(message) + 2
        current_last_row = row_number
    if current:
        segments.append("\n\n".join(current))
        segment_last_rows.append(current_last_row)
    if not segments:
        segments = ["[no analyzable user/assistant messages]"]
        segment_last_rows = [processed_message_count]

    # A provider timeout retries one smaller segment instead of repeating the
    # same expensive request. Successful partial batches advance the durable
    # cursor and the remaining context is picked up by the next queue pass.
    limit = 1 if retry_level else max(1, config.max_segments_per_session)
    segments = segments[:limit]
    processed_through = max(
        processed_message_count,
        segment_last_rows[len(segments) - 1],
    )
    session_meta = dict(session)
    if incremental:
        session_meta["incremental_update"] = {
            "prior_analyzed_messages": processed_message_count,
            "new_messages": count - processed_message_count,
            "context_messages": context_before,
        }
    if retry_level:
        session_meta["retry_compaction"] = {
            "attempt": retry_level + 1,
            "max_chars": max_chars,
        }
    if correction_examples:
        session_meta["user_correction_examples"] = correction_examples
    return session_meta, segments, source_hash, processed_through


def _create_run(
    run_type: str,
    provider: str,
    model: str,
    session_id: Optional[str],
    input_hash: str,
    prompt_version: str,
    request: dict,
) -> str:
    run_id = f"run-{uuid.uuid4().hex}"
    with get_db(busy_timeout_ms=_WORKER_DB_BUSY_TIMEOUT_MS) as conn:
        conn.execute(
            """INSERT INTO agent_runs(
                   id,run_type,provider,model,status,session_id,input_hash,
                   prompt_version,request_json,started_at)
               VALUES (?,?,?,?, 'running',?,?,?,?,?)""",
            (
                run_id, run_type, provider, model, session_id, input_hash,
                prompt_version, json.dumps(request, ensure_ascii=False), _now(),
            ),
        )
    return run_id


def _finish_run(
    run_id: str,
    status: str,
    duration_ms: int = 0,
    response: Optional[dict] = None,
    error: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    with get_db(busy_timeout_ms=_WORKER_DB_BUSY_TIMEOUT_MS) as conn:
        conn.execute(
            """UPDATE agent_runs SET status=?,response_json=?,error_message=?,
                      duration_ms=?,finished_at=?,model=COALESCE(?,model) WHERE id=?""",
            (
                status,
                json.dumps(response, ensure_ascii=False) if response is not None else None,
                error,
                duration_ms,
                _now(),
                model,
                run_id,
            ),
        )


def process_session(
    session_id: str,
    *,
    processed_message_count: int = 0,
    require_running_job: bool = True,
    retry_level: int = 0,
) -> dict:
    """Analyze one session synchronously. Called only from worker/CLI threads."""
    config = get_config().agent
    if not config.enabled:
        raise AgentProviderError("自动整理功能已关闭")
    provider = get_agent_provider()
    session, segments, source_hash, processed_through = _load_session_context(
        session_id, processed_message_count, retry_level=retry_level
    )
    extraction_scope = f"{source_hash[:16]}-{processed_message_count}"
    analyses: list[tuple[int, dict, str]] = []
    completed_runs: list[tuple[str, Any]] = []
    total_duration = 0
    for segment_index, segment in enumerate(segments):
        input_hash = hashlib.sha256(segment.encode("utf-8")).hexdigest()
        run_id = _create_run(
            "session_enrichment",
            provider.name,
            config.model or "codex-default",
            session_id,
            input_hash,
            SESSION_ANALYSIS_PROMPT_VERSION,
            {
                "segment_index": segment_index,
                "segment_count": len(segments),
                "chars": len(segment),
                "retry_level": retry_level,
            },
        )
        try:
            result = analyze_session_segment(
                provider, session, segment, segment_index, len(segments)
            )
            total_duration += result.duration_ms
            analyses.append((segment_index, result.data, result.model))
            completed_runs.append((run_id, result))
        except Exception as exc:
            _finish_run(run_id, "error", error=str(exc))
            for prior_run_id, prior_result in completed_runs:
                _finish_run(
                    prior_run_id,
                    "discarded",
                    prior_result.duration_ms,
                    prior_result.data,
                    error="同一批次的后续片段未完成",
                    model=prior_result.model,
                )
            raise
    try:
        item_ids = apply_session_analyses(
            session_id,
            analyses,
            SESSION_ANALYSIS_PROMPT_VERSION,
            config.auto_approve_threshold,
            extraction_scope,
            source_hash,
            require_running_job=require_running_job,
        )
    except Exception as exc:
        for run_id, result in completed_runs:
            _finish_run(
                run_id,
                "discarded",
                result.duration_ms,
                result.data,
                error=(
                    "会话在整理期间发生变化"
                    if isinstance(exc, StaleSessionAnalysisError)
                    else f"分析结果未能写入：{exc}"
                ),
                model=result.model,
            )
        raise
    for run_id, result in completed_runs:
        _finish_run(
            run_id,
            "success",
            result.duration_ms,
            result.data,
            model=result.model,
        )
    return {
        "session_id": session_id,
        "segments": len(segments),
        "work_item_ids": list(dict.fromkeys(item_ids)),
        "source_hash": source_hash,
        "processed_message_count": processed_through,
        "duration_ms": total_duration,
    }


def _claim_next_job() -> Optional[dict]:
    with get_db(busy_timeout_ms=_WORKER_DB_BUSY_TIMEOUT_MS) as conn:
        row = conn.execute(
            """SELECT j.* FROM enrichment_jobs j
               JOIN sessions s ON s.id=j.session_id
               WHERE j.status='pending'
                 AND (j.not_before IS NULL OR j.not_before <= ?)
               ORDER BY j.priority DESC,
                        COALESCE(julianday(s.end_time),julianday(s.start_time),
                                 julianday(j.updated_at),0) DESC,
                        julianday(j.updated_at) DESC,j.id DESC
               LIMIT 1""",
            (_now(),),
        ).fetchone()
        if not row:
            return None
        cursor = conn.execute(
            """UPDATE enrichment_jobs SET status='running',attempts=attempts+1,
                      started_at=?,error_message=NULL,updated_at=datetime('now')
               WHERE id=? AND status='pending'""",
            (_now(), row["id"]),
        )
        if cursor.rowcount != 1:
            return None
        return dict(row)


def _complete_job(job: dict, result: dict) -> None:
    with get_db(busy_timeout_ms=_WORKER_DB_BUSY_TIMEOUT_MS) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current_job = conn.execute(
            """SELECT status,processed_hash FROM enrichment_jobs WHERE id=?""",
            (job["id"],),
        ).fetchone()
        if not current_job or current_job["status"] != "running":
            return
        current_hash = compute_session_hash(conn, job["session_id"])
        if current_hash != result["source_hash"]:
            processed_message_count = (
                result["processed_message_count"]
                if current_job["processed_hash"] else 0
            )
            not_before = (
                datetime.now(timezone.utc)
                + timedelta(seconds=max(0, get_config().agent.settle_seconds))
            ).isoformat(timespec="seconds")
            conn.execute(
                """UPDATE enrichment_jobs SET status='pending',source_hash=?,
                           processed_hash=?,processed_message_count=?,attempts=0,
                           reason='messages_appended',not_before=?,finished_at=?,
                           updated_at=datetime('now')
                   WHERE id=? AND status='running'""",
                (
                    current_hash,
                    result["source_hash"],
                    processed_message_count,
                    not_before,
                    _now(),
                    job["id"],
                ),
            )
        else:
            status = (
                "completed"
                if result["processed_message_count"] >= int(conn.execute(
                    f"""SELECT COUNT(*) FROM messages WHERE session_id=?
                         AND role IN ({','.join('?' for _ in _ANALYZABLE_ROLES)})
                         AND COALESCE(msg_type,'') NOT IN ({','.join('?' for _ in _SKIP_MESSAGE_TYPES)})
                         AND length(trim(COALESCE(content,''))) > 0""",
                    (
                        job["session_id"], *_ANALYZABLE_ROLES,
                        *_SKIP_MESSAGE_TYPES,
                    ),
                ).fetchone()[0])
                else "pending"
            )
            next_processed_hash = (
                result["source_hash"] if status == "completed" else job.get("processed_hash")
            )
            conn.execute(
                """UPDATE enrichment_jobs SET status=?,processed_hash=?,
                           source_hash=?,processed_message_count=?,attempts=0,
                           finished_at=?,not_before=NULL,
                           error_message=NULL,updated_at=datetime('now')
                    WHERE id=? AND status='running' AND source_hash=?""",
                (
                    status,
                    next_processed_hash,
                    current_hash,
                    result["processed_message_count"],
                    _now(),
                    job["id"],
                    result["source_hash"],
                ),
            )


def _fail_job(job: dict, error: Exception) -> None:
    with get_db(busy_timeout_ms=_WORKER_DB_BUSY_TIMEOUT_MS) as conn:
        current = conn.execute(
            """SELECT status,attempts,source_hash,not_before
               FROM enrichment_jobs WHERE id=?""",
            (job["id"],),
        ).fetchone()
        if not current or current["status"] != "running":
            return
        stale_analysis = isinstance(error, StaleSessionAnalysisError)
        source_changed = current["source_hash"] != job.get("source_hash")
        transient_change = source_changed or stale_analysis
        current_hash = current["source_hash"]
        if stale_analysis:
            current_hash = compute_session_hash(conn, job["session_id"])
        attempts = 0 if transient_change else int(current["attempts"] or 0)
        error_text = str(error)
        provider_timeout = isinstance(error, AgentProviderError) and (
            "超时" in error_text or "timeout" in error_text.casefold()
        )
        max_attempts = 2 if provider_timeout else 3
        retry = transient_change or attempts < max_attempts
        not_before = None
        if retry and not_before is None:
            if transient_change:
                delay = timedelta(
                    seconds=max(0, get_config().agent.settle_seconds)
                )
            elif provider_timeout:
                delay = timedelta(minutes=min(60, 10 * max(1, attempts)))
            else:
                delay = timedelta(minutes=min(30, 2 ** attempts))
            not_before = (
                datetime.now(timezone.utc) + delay
            ).isoformat(timespec="seconds")
        conn.execute(
            """UPDATE enrichment_jobs SET status=?,attempts=?,source_hash=?,
                      error_message=?,not_before=?,started_at=NULL,
                      finished_at=?,updated_at=datetime('now')
               WHERE id=? AND status='running'""",
            (
                "pending" if retry else "failed",
                attempts,
                current_hash,
                None if transient_change else error_text[-4000:],
                not_before,
                _now(),
                job["id"],
            ),
        )


class EnrichmentManager:
    def __init__(self, scope_id: Optional[str] = None) -> None:
        self._scope_id = scope_id or get_data_scope()
        self._condition = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self._paused = False
        self._current_session_id: Optional[str] = None
        self._last_result: Optional[dict] = None
        self._last_error: Optional[str] = None
        self._provider_status_cache: Optional[dict] = None
        self._provider_status_checked_at = 0.0
        self._provider_status_lock = threading.Lock()
        self._started_once = False
        self._restart_count = 0
        self._last_started_at: Optional[str] = None
        self._last_heartbeat_at: Optional[str] = None

    def start(self) -> None:
        config = get_config().agent
        if not config.enabled or not config.auto_start_worker:
            return
        with self._condition:
            # Re-enabling an account may race a prior bounded stop request.
            # Clearing the flag lets an existing in-flight worker continue;
            # if it already exited, a replacement is started below.
            self._stopping = False
            if self._thread and self._thread.is_alive():
                self._condition.notify_all()
                return
            self._thread = threading.Thread(
                target=self._supervised_loop_for_scope,
                # Do not expose tenant identifiers through process diagnostics.
                name="work-enrichment",
                daemon=True,
            )
            self._started_once = True
            self._last_started_at = _now()
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread:
            thread.join(timeout=timeout)

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def pause(self) -> dict:
        with self._condition:
            self._paused = True
        return self.status()

    def resume(self) -> dict:
        with self._condition:
            self._paused = False
            self._condition.notify_all()
        self.start()
        return self.status()

    def enqueue_session(self, session_id: str, priority: int = 100, force: bool = False) -> dict:
        with get_db() as conn:
            if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise ValueError("Session not found")
            if not mark_session_pending(conn, session_id, "manual", priority):
                return {"status": "skipped", "session_id": session_id}
            if force:
                conn.execute(
                    """UPDATE enrichment_jobs SET status='pending',processed_hash=NULL,
                               processed_message_count=0,attempts=0,error_message=NULL,
                               not_before=NULL,priority=?,
                               updated_at=datetime('now') WHERE session_id=?""",
                    (priority, session_id),
                )
        self.wake()
        return {"status": "queued", "session_id": session_id}

    def enqueue_history(
        self, days: Optional[int] = None, source: Optional[str] = None, force: bool = False
    ) -> dict:
        changes = enqueue_backfill(days=days, source=source, force=force)
        self.wake()
        return {"status": "queued", "changes": changes, "days": days, "source": source}

    def retry_failed(self) -> int:
        with get_db(busy_timeout_ms=_WORKER_DB_BUSY_TIMEOUT_MS) as conn:
            cursor = conn.execute(
                """UPDATE enrichment_jobs SET status='pending',attempts=0,
                          not_before=NULL,error_message=NULL,updated_at=datetime('now')
                   WHERE status='failed'"""
            )
            count = cursor.rowcount
        self.wake()
        return count

    def invalidate_provider_status(self) -> None:
        """Force the next status request to probe the reloaded provider config."""
        with self._provider_status_lock:
            self._provider_status_cache = None
            self._provider_status_checked_at = 0.0

    def _provider_status(self) -> dict:
        now = time.monotonic()
        cached = self._provider_status_cache
        if cached is not None and now - self._provider_status_checked_at < _PROVIDER_STATUS_TTL_SECONDS:
            return dict(cached)

        # provider_status() launches ``codex --version``.  Polling clients must
        # never launch that subprocess concurrently: one slow executable probe
        # used to be able to consume the entire shared request thread pool.
        if not self._provider_status_lock.acquire(blocking=False):
            if cached is not None:
                return dict(cached)
            config = get_config().agent
            return {
                "enabled": config.enabled,
                "provider": config.provider,
                "available": None,
                "detail": "正在检查 Codex CLI",
                "model": config.model or "Codex 默认模型",
            }
        try:
            now = time.monotonic()
            if (
                self._provider_status_cache is None
                or now - self._provider_status_checked_at >= _PROVIDER_STATUS_TTL_SECONDS
            ):
                self._provider_status_cache = provider_status()
                self._provider_status_checked_at = now
            return dict(self._provider_status_cache)
        finally:
            self._provider_status_lock.release()

    def status(self, compact: bool = False) -> dict:
        value = get_enrichment_status(compact=compact)
        runtime = self.runtime_snapshot()
        if not compact:
            runtime["last_result"] = self._last_result
        value["runtime"] = runtime
        provider = self._provider_status()
        if compact:
            provider = {
                key: provider.get(key)
                for key in ("provider", "enabled", "available", "detail", "model")
            }
        value["provider"] = provider
        return value

    @property
    def started_once(self) -> bool:
        return self._started_once

    def runtime_snapshot(self) -> dict:
        """Return worker liveness without touching SQLite or probing a provider."""
        with self._condition:
            return {
                "worker_alive": bool(self._thread and self._thread.is_alive()),
                "worker_started": self._started_once,
                "paused": self._paused,
                "current_session_id": self._current_session_id,
                "last_error": self._last_error,
                "restart_count": self._restart_count,
                "last_started_at": self._last_started_at,
                "last_heartbeat_at": self._last_heartbeat_at,
            }

    def _supervised_loop_for_scope(self) -> None:
        # ContextVars do not cross regular thread boundaries. Keep this worker
        # permanently bound to the data space that owns its durable queue.
        with tenant_scope(self._scope_id):
            self._supervised_loop()

    def _supervised_loop(self) -> None:
        """Keep one unexpected iteration failure from permanently killing Agent."""
        delay = 1.0
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                self._loop()
            except Exception as exc:  # noqa: BLE001 - supervisor boundary
                self._current_session_id = None
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._restart_count += 1
                logger.exception(
                    "Work enrichment worker crashed; restarting in %.1fs", delay
                )
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(timeout=delay)
            delay = min(30.0, delay * 2)

    def _loop(self) -> None:
        logger.info("Work enrichment worker started")
        while True:
            self._last_heartbeat_at = _now()
            with self._condition:
                if self._stopping:
                    break
                if self._paused:
                    self._condition.wait(timeout=5)
                    continue
            try:
                job = _claim_next_job()
            except sqlite3.OperationalError as exc:
                self._last_error = str(exc)
                logger.warning("Enrichment queue is waiting for SQLite: %s", exc)
                with self._condition:
                    self._condition.wait(timeout=5)
                continue
            if not job:
                with self._condition:
                    self._condition.wait(timeout=5)
                continue
            self._current_session_id = job["session_id"]
            try:
                result = process_session(
                    job["session_id"],
                    processed_message_count=int(
                        job.get("processed_message_count") or 0
                    ),
                    retry_level=int(job.get("attempts") or 0),
                )
                _complete_job(job, result)
                self._last_result = result
                self._last_error = None
                logger.info(
                    "Enriched %s into %d work items",
                    job["session_id"], len(result["work_item_ids"]),
                )
            except Exception as exc:
                self._last_error = str(exc)
                try:
                    _fail_job(job, exc)
                except sqlite3.OperationalError:
                    logger.exception(
                        "Unable to persist enrichment failure for %s; worker will continue",
                        job["session_id"],
                    )
                if isinstance(exc, StaleSessionAnalysisError):
                    logger.info(
                        "Enrichment deferred because %s changed during analysis",
                        job["session_id"],
                    )
                else:
                    logger.exception("Enrichment failed for %s", job["session_id"])
            finally:
                self._current_session_id = None
        logger.info("Work enrichment worker stopped")


class TenantEnrichmentManagers:
    """Resolve one independent worker and runtime status per user data scope."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._managers: dict[str, EnrichmentManager] = {}

    def for_scope(self, scope: Optional[str] = None) -> EnrichmentManager:
        resolved = scope or get_data_scope()
        with self._lock:
            manager = self._managers.get(resolved)
            if manager is None:
                manager = EnrichmentManager(resolved)
                self._managers[resolved] = manager
            return manager

    def start(self) -> None:
        self.for_scope().start()

    def start_for_scope(self, scope: str) -> None:
        self.for_scope(scope).start()

    def stop_for_scope(self, scope: str, timeout: float = 5.0) -> None:
        self.for_scope(scope).stop(timeout)

    def start_all(self, scopes: list[str]) -> None:
        for scope in scopes:
            self.start_for_scope(scope)

    def stop(self, timeout: float = 5.0) -> None:
        self.for_scope().stop(timeout)

    def stop_all(self, timeout: float = 5.0) -> None:
        with self._lock:
            managers = list(self._managers.values())
        if not managers:
            return
        # Signal every worker before joining any one of them so shutdown time
        # remains bounded even when several users have idle workers.
        for manager in managers:
            with manager._condition:
                manager._stopping = True
                manager._condition.notify_all()
        deadline = time.monotonic() + max(0.0, timeout)
        for manager in managers:
            remaining = max(0.0, deadline - time.monotonic())
            thread = manager._thread
            if thread:
                thread.join(timeout=remaining)

    def wake(self) -> None:
        self.for_scope().wake()

    def pause(self) -> dict:
        return self.for_scope().pause()

    def resume(self) -> dict:
        return self.for_scope().resume()

    def enqueue_session(
        self, session_id: str, priority: int = 100, force: bool = False
    ) -> dict:
        return self.for_scope().enqueue_session(session_id, priority, force)

    def enqueue_history(
        self,
        days: Optional[int] = None,
        source: Optional[str] = None,
        force: bool = False,
    ) -> dict:
        return self.for_scope().enqueue_history(days, source, force)

    def retry_failed(self) -> int:
        return self.for_scope().retry_failed()

    def invalidate_provider_status(self) -> None:
        self.for_scope().invalidate_provider_status()

    def status(self, compact: bool = False) -> dict:
        return self.for_scope().status(compact)

    def runtime_snapshot(self) -> dict:
        return self.for_scope().runtime_snapshot()

    def aggregate_runtime(self) -> dict:
        with self._lock:
            snapshots = [
                manager.runtime_snapshot()
                for manager in self._managers.values()
            ]
        started = [item for item in snapshots if item.get("worker_started")]
        alive = [item for item in started if item.get("worker_alive")]
        return {
            "worker_started": bool(started),
            "worker_alive": bool(started) and len(alive) == len(started),
            "worker_count": len(started),
            "alive_count": len(alive),
            "paused_count": sum(1 for item in started if item.get("paused")),
            "restart_count": sum(
                int(item.get("restart_count") or 0) for item in started
            ),
        }


enrichment_manager = TenantEnrichmentManagers()
