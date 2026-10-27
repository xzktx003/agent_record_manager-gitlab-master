"""Derived work-memory storage and retrieval.

Raw conversations remain the immutable evidence source in :mod:`app.database`.
This module stores reviewable, AI-derived work objects on top of that source.
It intentionally uses SQLite only: at the current corpus size a relational
model plus FTS5 is easier to operate, back up and audit than a separate search
or graph service.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app.database import (
    build_resume_command,
    get_db,
    get_db_generation,
    get_db_path,
)


ACTIVE_STATUSES = ("planned", "open", "in_progress", "blocked")
WORK_KINDS = ("task", "decision", "solution", "knowledge", "idea")
WORK_STATUSES = (*ACTIVE_STATUSES, "done", "cancelled", "archived")
REVIEW_STATES = ("pending", "auto", "approved", "edited", "dismissed")
_SKIP_ENRICHMENT_MESSAGE_TYPES = (
    "reasoning", "tool_call", "function_call", "function_call_output",
    "execution_complete", "tool_output", "exec_command", "mcp_tool_call",
    "patch_apply", "web_search", "subagent_start",
)
JSON_FIELDS = {
    "next_steps": "next_steps_json",
    "keywords": "keywords_json",
    "metadata": "metadata_json",
}

_MAJOR_TASK_ACTION_TOKENS = frozenset({
    "add", "analyze", "analysis", "audit", "build", "check", "commit",
    "compare", "create", "debug", "delete", "deploy", "design", "diagnose",
    "evaluate", "evaluation", "export", "fix", "implement", "improve",
    "investigate", "merge", "migrate", "optimize", "plan", "prepare", "push",
    "refactor", "remove", "repair", "replace", "research", "review", "run",
    "submit", "summarize", "summary", "sync", "test", "update", "upgrade",
    "validate", "validation", "verify",
})
_MAJOR_TASK_GENERIC_TOKENS = frozenset({
    "task", "issue", "work", "project", "repo", "repository", "code",
})
_SUMMARY_BULLET_LIMIT = 4


class StaleSessionAnalysisError(RuntimeError):
    """The raw session changed or was excluded while analysis was running."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _slug(value: str, prefix: str) -> str:
    normalized = re.sub(r"[^a-z0-9\u3400-\u9fff]+", "-", (value or "").casefold())
    normalized = normalized.strip("-")[:48] or prefix
    digest = hashlib.sha1((value or prefix).encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{normalized}-{digest}"


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "-", (value or "").casefold()).strip("-")


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def init_work_memory_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    """Create the additive work-memory schema and FTS index."""
    if conn is None:
        with get_db() as owned_conn:
            init_work_memory_schema(owned_conn)
        return
    conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                summary TEXT,
                color TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS major_tasks (
                id TEXT PRIMARY KEY,
                group_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                objective TEXT,
                project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
                project_name TEXT,
                title_source TEXT NOT NULL DEFAULT 'heuristic',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_major_tasks_project
                ON major_tasks(project_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS work_items (
                id TEXT PRIMARY KEY,
                dedupe_key TEXT,
                kind TEXT NOT NULL DEFAULT 'task',
                title TEXT NOT NULL,
                project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
                project_name TEXT,
                major_task_id TEXT REFERENCES major_tasks(id) ON DELETE SET NULL,
                category TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'normal',
                summary TEXT,
                problem TEXT,
                root_cause TEXT,
                solution TEXT,
                outcome TEXT,
                next_steps_json TEXT NOT NULL DEFAULT '[]',
                keywords_json TEXT NOT NULL DEFAULT '[]',
                keywords_text TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                review_state TEXT NOT NULL DEFAULT 'pending',
                ai_generated INTEGER NOT NULL DEFAULT 1,
                merged_into_id TEXT REFERENCES work_items(id),
                source_start_time TEXT,
                source_end_time TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_work_items_status
                ON work_items(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_work_items_project
                ON work_items(project_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_work_items_review
                ON work_items(review_state, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_work_items_dedupe
                ON work_items(dedupe_key);
            CREATE INDEX IF NOT EXISTS idx_work_items_time
                ON work_items(source_end_time DESC, source_start_time DESC);

            CREATE TABLE IF NOT EXISTS work_item_sources (
                extraction_key TEXT PRIMARY KEY,
                work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                segment_index INTEGER NOT NULL DEFAULT 0,
                item_index INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_work_sources_session
                ON work_item_sources(session_id);
            CREATE INDEX IF NOT EXISTS idx_work_sources_item
                ON work_item_sources(work_item_id);

            CREATE TABLE IF NOT EXISTS work_item_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_key TEXT NOT NULL UNIQUE,
                work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                evidence_type TEXT NOT NULL DEFAULT 'support',
                excerpt TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_work_evidence_item
                ON work_item_evidence(work_item_id, id);
            CREATE INDEX IF NOT EXISTS idx_work_evidence_session
                ON work_item_evidence(session_id, message_id);

            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(entity_type, normalized_name)
            );

            CREATE TABLE IF NOT EXISTS work_item_entities (
                work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                PRIMARY KEY(work_item_id, entity_id)
            );

            CREATE TABLE IF NOT EXISTS work_item_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                to_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
                relation_type TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1,
                ai_generated INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(from_item_id, to_item_id, relation_type)
            );

            CREATE TABLE IF NOT EXISTS session_insights (
                session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                summary TEXT,
                project_name TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                analysis_json TEXT NOT NULL DEFAULT '{}',
                source_hash TEXT,
                prompt_version TEXT,
                model TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS enrichment_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                source_hash TEXT,
                processed_hash TEXT,
                processed_message_count INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                not_before TEXT,
                started_at TEXT,
                finished_at TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_enrichment_queue
                ON enrichment_jobs(status, priority DESC, id ASC);
            CREATE INDEX IF NOT EXISTS idx_enrichment_queue_recent
                ON enrichment_jobs(status, priority DESC, updated_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS agent_runs (
                id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                status TEXT NOT NULL,
                session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
                work_item_id TEXT REFERENCES work_items(id) ON DELETE SET NULL,
                input_hash TEXT,
                prompt_version TEXT,
                request_json TEXT,
                response_json TEXT,
                error_message TEXT,
                duration_ms INTEGER,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_agent_runs_recent
                ON agent_runs(started_at DESC);

            CREATE TABLE IF NOT EXISTS work_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_item_id TEXT REFERENCES work_items(id) ON DELETE SET NULL,
                action TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY,
                period_type TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                title TEXT NOT NULL,
                content_markdown TEXT NOT NULL,
                data_json TEXT NOT NULL DEFAULT '{}',
                ai_generated INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(period_type, period_start, period_end)
            );

            CREATE TABLE IF NOT EXISTS saved_searches (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                query TEXT NOT NULL,
                filters_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS smart_search_jobs (
                id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                filters_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                plan_json TEXT,
                result_json TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                started_at TEXT,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_smart_search_recent
                ON smart_search_jobs(created_at DESC);

            CREATE TABLE IF NOT EXISTS work_memory_meta (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(enrichment_jobs)")
    }
    if "processed_message_count" not in columns:
        conn.execute(
            "ALTER TABLE enrichment_jobs ADD COLUMN "
            "processed_message_count INTEGER NOT NULL DEFAULT 0"
        )
    work_item_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(work_items)")
    }
    if "major_task_id" not in work_item_columns:
        conn.execute(
            "ALTER TABLE work_items ADD COLUMN major_task_id TEXT "
            "REFERENCES major_tasks(id) ON DELETE SET NULL"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_items_major_task "
        "ON work_items(major_task_id, updated_at DESC)"
    )
    _ensure_work_fts(conn)
    _backfill_major_task_assignments(conn)


def recover_interrupted_agent_state() -> None:
    """Recover durable jobs when the owning Web process starts.

    This intentionally is not part of schema initialization: CLI readers may
    run alongside the service and must not reset a legitimate in-flight job.
    The deployment uses one enrichment worker process for the SQLite queue.
    """
    with get_db() as conn:
        _skip_guardian_enrichment_jobs(conn)
        _backfill_processed_message_counts(conn)
        conn.execute(
            "UPDATE enrichment_jobs SET status='pending', started_at=NULL "
            "WHERE status='running'"
        )
        # Preserve interrupted audit rows, but do not show them as running
        # forever after a restart.
        conn.execute(
            """UPDATE agent_runs SET status='error',
                      error_message=COALESCE(error_message,'服务重启，未完成的自动整理已中断'),
                      finished_at=COALESCE(finished_at,datetime('now'))
               WHERE status='running'"""
        )
        conn.execute(
            """UPDATE smart_search_jobs SET status='failed',
                      error_message=COALESCE(error_message,'服务重启，搜索任务已中断'),
                      finished_at=COALESCE(finished_at,datetime('now'))
               WHERE status IN ('queued','running')"""
        )


def _ensure_work_fts(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='work_items_fts'"
    ).fetchone()
    if row and "trigram" in (row[0] or "") and "dedupe_key" in (row[0] or ""):
        return
    conn.executescript(
        """
        DROP TABLE IF EXISTS work_items_fts;
        DROP TRIGGER IF EXISTS work_items_fts_ai;
        DROP TRIGGER IF EXISTS work_items_fts_ad;
        DROP TRIGGER IF EXISTS work_items_fts_au;

        CREATE VIRTUAL TABLE work_items_fts USING fts5(
            title, dedupe_key, project_name, category, summary, problem, root_cause,
            solution, outcome, next_steps_json, keywords_text,
            content=work_items, content_rowid=rowid,
            tokenize='trigram', detail='none', columnsize=0
        );

        CREATE TRIGGER work_items_fts_ai AFTER INSERT ON work_items BEGIN
            INSERT INTO work_items_fts(
                rowid, title, dedupe_key, project_name, category, summary, problem,
                root_cause, solution, outcome, next_steps_json, keywords_text
            ) VALUES (
                new.rowid, new.title, new.dedupe_key, new.project_name, new.category,
                new.summary, new.problem, new.root_cause, new.solution,
                new.outcome, new.next_steps_json, new.keywords_text
            );
        END;
        CREATE TRIGGER work_items_fts_ad AFTER DELETE ON work_items BEGIN
            INSERT INTO work_items_fts(
                work_items_fts, rowid, title, dedupe_key, project_name, category, summary,
                problem, root_cause, solution, outcome, next_steps_json,
                keywords_text
            ) VALUES (
                'delete', old.rowid, old.title, old.dedupe_key, old.project_name, old.category,
                old.summary, old.problem, old.root_cause, old.solution,
                old.outcome, old.next_steps_json, old.keywords_text
            );
        END;
        CREATE TRIGGER work_items_fts_au AFTER UPDATE OF
            title, dedupe_key, project_name, category, summary, problem,
            root_cause, solution, outcome, next_steps_json, keywords_text
        ON work_items BEGIN
            INSERT INTO work_items_fts(
                work_items_fts, rowid, title, dedupe_key, project_name, category, summary,
                problem, root_cause, solution, outcome, next_steps_json,
                keywords_text
            ) VALUES (
                'delete', old.rowid, old.title, old.dedupe_key, old.project_name, old.category,
                old.summary, old.problem, old.root_cause, old.solution,
                old.outcome, old.next_steps_json, old.keywords_text
            );
            INSERT INTO work_items_fts(
                rowid, title, dedupe_key, project_name, category, summary, problem,
                root_cause, solution, outcome, next_steps_json, keywords_text
            ) VALUES (
                new.rowid, new.title, new.dedupe_key, new.project_name, new.category,
                new.summary, new.problem, new.root_cause, new.solution,
                new.outcome, new.next_steps_json, new.keywords_text
            );
        END;
        """
    )
    count = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
    if count:
        conn.execute(
            """INSERT INTO work_items_fts(
                   rowid, title, dedupe_key, project_name, category, summary, problem,
                   root_cause, solution, outcome, next_steps_json, keywords_text)
               SELECT rowid, title, dedupe_key, project_name, category, summary, problem,
                      root_cause, solution, outcome, next_steps_json, keywords_text
               FROM work_items"""
        )


def compute_session_hash(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute(
        """SELECT s.id, s.title, s.message_count,
                  COALESCE(MAX(m.id), 0), COALESCE(SUM(length(m.content)), 0)
           FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
           WHERE s.id = ? GROUP BY s.id""",
        (session_id,),
    ).fetchone()
    if not row:
        return ""
    # ``sessions.updated_at`` changes on harmless metadata refreshes. Including
    # it caused unchanged conversations to be re-analysed and made long-running
    # results stale every time the five-minute sync touched the row.
    return hashlib.sha256("|".join(str(value or "") for value in row).encode()).hexdigest()


def _is_guardian_metadata(raw_metadata: Optional[str]) -> bool:
    try:
        metadata = json.loads(raw_metadata or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    source = metadata.get("source") if isinstance(metadata, dict) else None
    subagent = source.get("subagent") if isinstance(source, dict) else None
    return (
        isinstance(subagent, dict)
        and str(subagent.get("other") or "").casefold() == "guardian"
    )


def _mark_guardian_skipped(
    conn: sqlite3.Connection,
    session_id: str,
    source_hash: Optional[str] = None,
) -> None:
    source_hash = source_hash or compute_session_hash(conn, session_id)
    conn.execute(
        """INSERT INTO enrichment_jobs(
               session_id,status,priority,reason,source_hash,processed_hash,
               processed_message_count,attempts,created_at,updated_at)
           VALUES (?, 'skipped', 0, 'guardian', ?, ?, 0, 0,
                   datetime('now'),datetime('now'))
           ON CONFLICT(session_id) DO UPDATE SET
               status='skipped',reason='guardian',source_hash=excluded.source_hash,
               processed_hash=excluded.processed_hash,processed_message_count=0,
               attempts=0,not_before=NULL,
               started_at=NULL,finished_at=datetime('now'),error_message=NULL,
               updated_at=datetime('now')""",
        (session_id, source_hash, source_hash),
    )
    _purge_session_derived_data(conn, session_id)


def _purge_session_derived_data(
    conn: sqlite3.Connection, session_id: str
) -> None:
    """Remove Agent-derived data for one excluded session, never its raw data."""
    items = conn.execute(
        """SELECT DISTINCT w.id,w.ai_generated,w.review_state
           FROM work_items w JOIN (
               SELECT work_item_id FROM work_item_sources WHERE session_id=?
               UNION SELECT work_item_id FROM work_item_evidence WHERE session_id=?
           ) linked ON linked.work_item_id=w.id""",
        (session_id, session_id),
    ).fetchall()
    conn.execute("DELETE FROM session_insights WHERE session_id=?", (session_id,))
    for item in items:
        user_curated = (
            not bool(item["ai_generated"])
            or item["review_state"] in ("approved", "edited")
        )
        if user_curated:
            continue
        item_id = item["id"]
        conn.execute(
            "DELETE FROM work_item_sources WHERE session_id=? AND work_item_id=?",
            (session_id, item_id),
        )
        conn.execute(
            "DELETE FROM work_item_evidence WHERE session_id=? AND work_item_id=?",
            (session_id, item_id),
        )
        linked_elsewhere = conn.execute(
            """SELECT 1 FROM work_item_sources WHERE work_item_id=?
               UNION SELECT 1 FROM work_item_evidence WHERE work_item_id=?
               LIMIT 1""",
            (item_id, item_id),
        ).fetchone()
        if not linked_elsewhere:
            conn.execute(
                "DELETE FROM work_items WHERE id=? AND ai_generated=1",
                (item_id,),
            )


def _skip_guardian_enrichment_jobs(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """SELECT j.session_id,s.metadata_json
           FROM enrichment_jobs j JOIN sessions s ON s.id=j.session_id
           WHERE j.status!='skipped' AND s.metadata_json IS NOT NULL"""
    ).fetchall()
    guardian_ids = [
        row["session_id"]
        for row in rows
        if _is_guardian_metadata(row["metadata_json"])
    ]
    for session_id in guardian_ids:
        _mark_guardian_skipped(conn, session_id)
    return len(guardian_ids)


def _infer_processed_message_count(
    conn: sqlite3.Connection, session_id: str
) -> int:
    """Best-effort migration cursor for jobs completed before cursors existed."""
    parsed_runs = []
    for row in conn.execute(
        """SELECT started_at,finished_at,request_json FROM agent_runs
           WHERE session_id=? AND run_type='session_enrichment'
             AND status='success' AND finished_at IS NOT NULL
           ORDER BY finished_at DESC""",
        (session_id,),
    ).fetchall():
        try:
            request = json.loads(row["request_json"] or "{}")
            index = int(request.get("segment_index", -1))
            count = int(request.get("segment_count", 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if count > 0 and 0 <= index < count:
            parsed_runs.append((index, count, row["started_at"], row["finished_at"]))

    snapshot_at = None
    for position, (index, count, started_at, _finished_at) in enumerate(parsed_runs):
        if index != count - 1:
            continue
        expected = index
        next_started_at = None
        for (
            candidate_index,
            candidate_count,
            candidate_started_at,
            candidate_finished_at,
        ) in parsed_runs[position:]:
            if candidate_count != count:
                continue
            if candidate_index > expected:
                break
            if candidate_index != expected:
                continue
            if next_started_at and candidate_finished_at != next_started_at:
                break
            snapshot_at = candidate_started_at
            next_started_at = candidate_started_at
            expected -= 1
            if expected < 0:
                break
        if expected < 0:
            break
        snapshot_at = None
    if not snapshot_at:
        return 0
    source = conn.execute(
        "SELECT source FROM sessions WHERE id=?", (session_id,)
    ).fetchone()
    if source and source["source"] == "opencode":
        return 0
    time_expression = "created_at"
    placeholders = ",".join("?" for _ in _SKIP_ENRICHMENT_MESSAGE_TYPES)
    return int(conn.execute(
        f"""SELECT COUNT(*) FROM messages WHERE session_id=?
              AND role IN ('user','assistant')
              AND COALESCE(msg_type,'') NOT IN ({placeholders})
              AND length(trim(COALESCE(content,''))) > 0
              AND julianday({time_expression}) <= julianday(?)""",
        (session_id, *_SKIP_ENRICHMENT_MESSAGE_TYPES, snapshot_at),
    ).fetchone()[0])


def _backfill_processed_message_counts(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """SELECT session_id FROM enrichment_jobs
           WHERE processed_message_count=0 AND processed_hash IS NOT NULL
             AND status IN ('pending','failed')"""
    ).fetchall()
    changed = 0
    for row in rows:
        count = _infer_processed_message_count(conn, row["session_id"])
        if count <= 0:
            continue
        conn.execute(
            """UPDATE enrichment_jobs SET processed_message_count=?,attempts=0,
                       status=CASE WHEN status='failed' THEN 'pending' ELSE status END,
                       not_before=NULL,error_message=NULL
               WHERE session_id=?""",
            (count, row["session_id"]),
        )
        changed += 1
    return changed


def mark_session_pending(
    conn: sqlite3.Connection,
    session_id: str,
    reason: str = "sync",
    priority: int = 0,
) -> bool:
    """Idempotently schedule a changed session if the derived schema exists."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrichment_jobs'"
    ).fetchone()
    if not exists:
        return False
    source_row = None
    try:
        from app.config import get_config
        agent_config = get_config().agent
        source_row = conn.execute(
            "SELECT source,metadata_json FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        source_name = source_row["source"] if source_row else ""
        if agent_config.allowed_sources and source_name not in agent_config.allowed_sources:
            return False
        if source_name in agent_config.excluded_sources:
            return False
    except (AttributeError, sqlite3.Error):
        agent_config = None
    source_hash = compute_session_hash(conn, session_id)
    if source_row and _is_guardian_metadata(source_row["metadata_json"]):
        _mark_guardian_skipped(conn, session_id, source_hash)
        return False
    existing_job = conn.execute(
        """SELECT source_hash,processed_hash,processed_message_count
           FROM enrichment_jobs WHERE session_id=?""",
        (session_id,),
    ).fetchone()
    if (
        existing_job
        and not int(existing_job["processed_message_count"] or 0)
        and existing_job["processed_hash"]
        and existing_job["source_hash"] != source_hash
    ):
        inferred_count = _infer_processed_message_count(conn, session_id)
        if inferred_count:
            conn.execute(
                """UPDATE enrichment_jobs SET processed_message_count=?
                   WHERE session_id=?""",
                (inferred_count, session_id),
            )
    delay_seconds = 0
    if reason in ("sync", "messages_appended", "messages_imported"):
        try:
            delay_seconds = max(
                0,
                int(agent_config.settle_seconds if agent_config else 300),
            )
        except (AttributeError, TypeError, ValueError):
            delay_seconds = 300
    not_before = (
        datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    ).isoformat(timespec="seconds") if delay_seconds else None
    conn.execute(
        """INSERT INTO enrichment_jobs(
               session_id, status, priority, reason, source_hash, not_before,
               created_at, updated_at)
           VALUES (?, 'pending', ?, ?, ?, ?, datetime('now'), datetime('now'))
           ON CONFLICT(session_id) DO UPDATE SET
               status = CASE
                   WHEN enrichment_jobs.status = 'running'
                        THEN 'running'
                   WHEN enrichment_jobs.processed_hash = excluded.source_hash
                        THEN enrichment_jobs.status
                   ELSE 'pending'
               END,
               priority = MAX(enrichment_jobs.priority, excluded.priority),
               reason = excluded.reason,
               source_hash = excluded.source_hash,
               not_before = CASE
                   WHEN enrichment_jobs.processed_hash = excluded.source_hash
                        THEN enrichment_jobs.not_before
                   ELSE excluded.not_before
               END,
                error_message = CASE
                    WHEN enrichment_jobs.processed_hash = excluded.source_hash
                         THEN enrichment_jobs.error_message
                    ELSE NULL
                END,
                attempts = CASE
                    WHEN COALESCE(enrichment_jobs.source_hash,'') !=
                         COALESCE(excluded.source_hash,'') THEN 0
                    ELSE enrichment_jobs.attempts
                END,
                updated_at = datetime('now')""",
        (session_id, priority, reason, source_hash, not_before),
    )
    return True


def enqueue_backfill(
    days: Optional[int] = None,
    source: Optional[str] = None,
    priority: int = -10,
    force: bool = False,
) -> int:
    with get_db() as conn:
        # Do not spend a Codex call on empty/import-metadata-only sessions.
        clauses = [
            "EXISTS (SELECT 1 FROM messages m WHERE m.session_id=s.id "
            "AND m.role IN ('user','assistant') "
            "AND length(trim(COALESCE(m.content,''))) > 0)"
        ]
        params: list[Any] = []
        if days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            clauses.append("COALESCE(s.end_time, s.start_time) >= ?")
            params.append(cutoff)
        if source:
            clauses.append("s.source = ?")
            params.append(source)
        rows = conn.execute(
            f"SELECT s.id FROM sessions s WHERE {' AND '.join(clauses)} "
            "ORDER BY COALESCE(s.end_time, s.start_time) DESC",
            params,
        ).fetchall()
        queued = 0
        for row in rows:
            scheduled = mark_session_pending(conn, row["id"], "backfill", priority)
            if not scheduled:
                continue
            queued += 1
            if force:
                conn.execute(
                    """UPDATE enrichment_jobs SET status='pending',
                               processed_hash=NULL, error_message=NULL,
                               processed_message_count=0,attempts=0,not_before=NULL,
                               updated_at=datetime('now') WHERE session_id=?""",
                    (row["id"],),
                )
        return queued


def get_enrichment_status(limit: int = 30, compact: bool = False) -> dict:
    with get_db() as conn:
        counts = conn.execute(
            "SELECT status, COUNT(*) AS count FROM enrichment_jobs GROUP BY status"
        ).fetchall()
        result = {"counts": {row["status"]: row["count"] for row in counts}}
        if compact:
            return result
        jobs = conn.execute(
            """SELECT j.*, s.title, s.source, s.server_name,
                      COALESCE(s.end_time, s.start_time) AS session_time
               FROM enrichment_jobs j JOIN sessions s ON s.id=j.session_id
               ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'failed' THEN 1
                         WHEN 'pending' THEN 2 ELSE 3 END,
                        j.priority DESC, j.updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        last_runs = conn.execute(
            """SELECT id, run_type, provider, model, status, session_id,
                      error_message, duration_ms, started_at, finished_at
               FROM agent_runs ORDER BY started_at DESC LIMIT 15"""
        ).fetchall()
        result["jobs"] = [dict(row) for row in jobs]
        result["recent_runs"] = [dict(row) for row in last_runs]
        return result


def _project_id(conn: sqlite3.Connection, name: Optional[str]) -> Optional[str]:
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute("SELECT id FROM projects WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    project_id = _slug(name, "prj")
    conn.execute(
        "INSERT OR IGNORE INTO projects(id, name) VALUES (?, ?)",
        (project_id, name),
    )
    row = conn.execute("SELECT id FROM projects WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return row["id"] if row else project_id


def _major_task_identity(item: dict) -> tuple[str, str, str, str]:
    """Return stable key, title, objective and title provenance for one item.

    New Agent results provide an explicit parent identity.  Historical rows are
    grouped deterministically from their dedupe key so the migration is local,
    repeatable and does not require re-sending years of conversations to a
    model.  The heuristic deliberately creates only one hierarchy level.
    """
    project = str(item.get("project") or item.get("project_name") or "").strip()
    normalized_project = _normalize_key(project)
    explicit_key = _normalize_key(str(item.get("major_task_key") or ""))
    explicit_title = str(item.get("major_task_title") or "").strip()[:300]
    explicit_objective = str(item.get("major_task_summary") or "").strip()[:2000]
    if explicit_key or explicit_title:
        topic_key = explicit_key or _normalize_key(explicit_title)
        # The extraction contract recommends ``project|objective``. Keep our
        # own canonical separator so explicit keys and historical heuristic
        # keys can converge instead of creating two visually identical parents.
        if normalized_project and topic_key.startswith(normalized_project + "-"):
            topic_key = topic_key[len(normalized_project) + 1:]
        elif topic_key == normalized_project:
            topic_key = "general"
        group_key = "|".join(
            value for value in (normalized_project, topic_key) if value
        )
        return (
            group_key or topic_key or "unclassified",
            explicit_title or str(item.get("title") or "未命名大任务")[:300],
            explicit_objective,
            "agent",
        )

    dedupe = str(item.get("dedupe_key") or "").casefold().strip()
    pipe_parts = [
        _normalize_key(part) for part in dedupe.split("|") if _normalize_key(part)
    ]
    if len(pipe_parts) >= 2:
        # Existing structured keys use object|activity|problem.  The first two
        # components describe the durable body of work; the last component is
        # usually the individual finding that belongs underneath it.
        topic_key = "-".join(pipe_parts[:2])
    else:
        tokens = [
            token for token in re.split(r"[^a-z0-9㐀-鿿]+", _normalize_key(dedupe))
            if token
        ]
        project_tokens = [token for token in normalized_project.split("-") if token]
        if project_tokens and tokens[:len(project_tokens)] == project_tokens:
            tokens = tokens[len(project_tokens):]
        elif normalized_project and tokens:
            # Project labels such as ``LinearAttentionResearch`` often appear
            # hyphenated in a dedupe key (``linear-attention-research-*``).
            # Strip that equivalent prefix before selecting the actual goal.
            compact_project = normalized_project.replace("-", "")
            joined_prefix = ""
            consumed = 0
            for index, token in enumerate(tokens[:8]):
                joined_prefix += token
                if joined_prefix == compact_project:
                    consumed = index + 1
                    break
                if not compact_project.startswith(joined_prefix):
                    break
            if consumed:
                tokens = tokens[consumed:]
        while len(tokens) > 1 and tokens[0] in _MAJOR_TASK_GENERIC_TOKENS:
            tokens.pop(0)
        boundary = next(
            (
                index for index, token in enumerate(tokens)
                # Keep the leading object + activity together (for example
                # ``jira-create`` or ``model-build``).  Cutting at the first
                # verb produces parents as broad as merely "Jira" or "Model".
                if index >= 2 and token in _MAJOR_TASK_ACTION_TOKENS
            ),
            len(tokens),
        )
        topic_tokens = tokens[:boundary]
        if len(topic_tokens) > 3:
            if (
                re.fullmatch(r"[a-z]{2,8}", topic_tokens[0] or "")
                and re.fullmatch(r"\d+[a-z]*", topic_tokens[1] or "")
            ):
                topic_tokens = topic_tokens[:2]
            elif any(character.isdigit() for character in topic_tokens[0]):
                topic_tokens = topic_tokens[:3]
            else:
                topic_tokens = topic_tokens[:3]
        if not topic_tokens:
            fallback = _normalize_key(
                str(item.get("category") or item.get("title") or "unclassified")
            )
            topic_tokens = fallback.split("-")[:3]
        topic_key = "-".join(topic_tokens) or "unclassified"

    group_key = "|".join(
        value for value in (normalized_project, topic_key) if value
    ) or topic_key
    title = str(item.get("title") or "未命名大任务").strip()[:300]
    objective = str(item.get("problem") or item.get("summary") or "").strip()[:2000]
    return group_key, title, objective, "heuristic"


def _ensure_major_task(
    conn: sqlite3.Connection,
    item: dict,
    *,
    project_id: Optional[str] = None,
) -> str:
    group_key, title, objective, title_source = _major_task_identity(item)
    row = conn.execute(
        "SELECT id,title_source,objective FROM major_tasks WHERE group_key=?",
        (group_key,),
    ).fetchone()
    project_name = str(item.get("project") or item.get("project_name") or "").strip()
    project_id = project_id if project_id is not None else _project_id(conn, project_name)
    if row:
        major_task_id = row["id"]
        # Explicit Agent hierarchy may safely improve a legacy heuristic label,
        # but never overwrite a title the user edited later.
        if title_source == "agent" and row["title_source"] == "heuristic":
            conn.execute(
                """UPDATE major_tasks SET title=?,objective=COALESCE(NULLIF(?,''),objective),
                          project_id=COALESCE(?,project_id),
                          project_name=COALESCE(NULLIF(?,''),project_name),
                          title_source='agent',updated_at=datetime('now') WHERE id=?""",
                (title, objective, project_id, project_name, major_task_id),
            )
        elif objective and not (row["objective"] or "").strip():
            conn.execute(
                "UPDATE major_tasks SET objective=?,updated_at=datetime('now') WHERE id=?",
                (objective, major_task_id),
            )
        return major_task_id

    major_task_id = _slug(group_key, "mt")
    conn.execute(
        """INSERT OR IGNORE INTO major_tasks(
               id,group_key,title,objective,project_id,project_name,title_source)
           VALUES (?,?,?,?,?,?,?)""",
        (
            major_task_id, group_key, title or "未命名大任务", objective,
            project_id, project_name, title_source,
        ),
    )
    row = conn.execute(
        "SELECT id FROM major_tasks WHERE group_key=?", (group_key,)
    ).fetchone()
    return row["id"] if row else major_task_id


def _backfill_major_task_assignments(conn: sqlite3.Connection) -> int:
    """One-time/additive local grouping for rows created by older versions."""
    rows = conn.execute(
        """SELECT * FROM work_items
           WHERE major_task_id IS NULL AND merged_into_id IS NULL
             AND review_state!='dismissed'
           ORDER BY updated_at DESC,id"""
    ).fetchall()
    if not rows:
        return 0
    assignments = []
    for row in rows:
        item = dict(row)
        item["project"] = item.get("project_name") or ""
        major_task_id = _ensure_major_task(
            conn, item, project_id=item.get("project_id")
        )
        assignments.append((major_task_id, item["id"]))
    conn.executemany(
        "UPDATE work_items SET major_task_id=? WHERE id=?", assignments
    )
    conn.execute(
        """INSERT INTO work_memory_meta(key,value,updated_at)
           VALUES ('major_task_backfill_v1',?,datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
             updated_at=excluded.updated_at""",
        (str(len(assignments)),),
    )
    return len(assignments)


def _clean_item(item: dict) -> dict:
    kind = item.get("kind") if item.get("kind") in WORK_KINDS else "task"
    status = item.get("status") if item.get("status") in WORK_STATUSES else "open"
    confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
    keywords = [str(value).strip() for value in (item.get("keywords") or []) if str(value).strip()]
    steps = [str(value).strip() for value in (item.get("next_steps") or []) if str(value).strip()]
    title = str(item.get("title") or item.get("summary") or "未命名任务").strip()[:300]
    dedupe_key = _normalize_key(item.get("dedupe_key") or "")
    if not dedupe_key:
        dedupe_key = _normalize_key("|".join([item.get("project") or "", title]))
    return {
        "kind": kind,
        "title": title,
        "project": str(item.get("project") or "").strip()[:200],
        "category": str(item.get("category") or "other").strip()[:100],
        "status": status,
        "priority": str(item.get("priority") or "normal").strip()[:30],
        "summary": str(item.get("summary") or "").strip(),
        "problem": str(item.get("problem") or "").strip(),
        "root_cause": str(item.get("root_cause") or "").strip(),
        "solution": str(item.get("solution") or "").strip(),
        "outcome": str(item.get("outcome") or "").strip(),
        "next_steps": steps,
        "keywords": keywords,
        "confidence": confidence,
        "dedupe_key": dedupe_key[:500],
        "major_task_id": str(item.get("major_task_id") or "").strip(),
        "major_task_key": str(item.get("major_task_key") or "").strip()[:500],
        "major_task_title": str(item.get("major_task_title") or "").strip()[:300],
        "major_task_summary": str(item.get("major_task_summary") or "").strip()[:2000],
        "entities": item.get("entities") or [],
        "evidence_message_ids": item.get("evidence_message_ids") or [],
    }


def _apply_session_analysis_conn(
    conn: sqlite3.Connection,
    session_id: str,
    analysis: dict,
    segment_index: int,
    prompt_version: str,
    model: str,
    auto_approve_threshold: float = 0.92,
    extraction_scope: str = "",
) -> list[str]:
    session = conn.execute(
            "SELECT start_time, end_time FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not session:
        return []
    summary = str(analysis.get("session_summary") or "").strip()
    tags = analysis.get("session_tags") or []
    conn.execute(
            """INSERT INTO session_insights(
                   session_id, summary, project_name, tags_json, analysis_json,
                   source_hash, prompt_version, model, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(session_id) DO UPDATE SET
                   summary=CASE WHEN excluded.summary != '' THEN excluded.summary
                                ELSE session_insights.summary END,
                   project_name=CASE WHEN excluded.project_name != ''
                                     THEN excluded.project_name
                                     ELSE session_insights.project_name END,
                   tags_json=excluded.tags_json,
                   analysis_json=excluded.analysis_json,
                   source_hash=excluded.source_hash,
                   prompt_version=excluded.prompt_version,
                   model=excluded.model,
                   updated_at=datetime('now')""",
            (
                session_id,
                summary,
                str(analysis.get("primary_project") or ""),
                _dump(tags),
                _dump(analysis),
                compute_session_hash(conn, session_id),
                prompt_version,
                model,
            ),
        )
    created_or_updated: list[str] = []
    for item_index, raw_item in enumerate(analysis.get("items") or []):
            item = _clean_item(raw_item)
            scope = f":{extraction_scope}" if extraction_scope else ""
            identity = item["dedupe_key"] or f"position-{item_index}"
            identity_hash = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
            extraction_key = f"{session_id}{scope}:{segment_index}:{identity_hash}"
            mapping = conn.execute(
                "SELECT work_item_id FROM work_item_sources WHERE extraction_key=?",
                (extraction_key,),
            ).fetchone()
            existing = None
            if mapping:
                existing = conn.execute(
                    "SELECT * FROM work_items WHERE id=?", (mapping["work_item_id"],)
                ).fetchone()
            if not existing and item["dedupe_key"]:
                existing = conn.execute(
                    """SELECT * FROM work_items
                       WHERE dedupe_key=? AND merged_into_id IS NULL
                         AND review_state != 'dismissed'
                       ORDER BY confidence DESC, updated_at DESC LIMIT 1""",
                    (item["dedupe_key"],),
                ).fetchone()

            project_id = _project_id(conn, item["project"])
            major_task_id = _ensure_major_task(
                conn, item, project_id=project_id
            )
            review_state = (
                "auto" if item["confidence"] >= auto_approve_threshold else "pending"
            )
            if existing:
                work_item_id = existing["id"]
                if existing["review_state"] not in ("edited", "approved"):
                    conn.execute(
                        """UPDATE work_items SET
                               dedupe_key=?, kind=?, title=?, project_id=?,
                               project_name=?, major_task_id=?, category=?, status=?, priority=?,
                               summary=?, problem=?, root_cause=?, solution=?,
                               outcome=?, next_steps_json=?, keywords_json=?,
                               keywords_text=?, confidence=MAX(confidence, ?),
                               review_state=CASE WHEN review_state='pending'
                                                 THEN ? ELSE review_state END,
                               source_start_time=COALESCE(source_start_time, ?),
                               source_end_time=MAX(COALESCE(source_end_time, ''), ?),
                               updated_at=datetime('now') WHERE id=?""",
                        (
                            item["dedupe_key"], item["kind"], item["title"],
                            project_id, item["project"], major_task_id, item["category"],
                            item["status"], item["priority"], item["summary"],
                            item["problem"], item["root_cause"], item["solution"],
                            item["outcome"], _dump(item["next_steps"]),
                            _dump(item["keywords"]), " ".join(item["keywords"]),
                            item["confidence"], review_state,
                            session["start_time"], session["end_time"] or session["start_time"],
                            work_item_id,
                        ),
                    )
            else:
                work_item_id = f"wi-{uuid.uuid4().hex}"
                conn.execute(
                    """INSERT INTO work_items(
                           id, dedupe_key, kind, title, project_id, project_name, major_task_id,
                           category, status, priority, summary, problem, root_cause,
                           solution, outcome, next_steps_json, keywords_json,
                           keywords_text, confidence, review_state, ai_generated,
                           source_start_time, source_end_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, 1, ?, ?)""",
                    (
                        work_item_id, item["dedupe_key"], item["kind"], item["title"],
                        project_id, item["project"], major_task_id, item["category"], item["status"],
                        item["priority"], item["summary"], item["problem"],
                        item["root_cause"], item["solution"], item["outcome"],
                        _dump(item["next_steps"]), _dump(item["keywords"]),
                        " ".join(item["keywords"]), item["confidence"], review_state,
                        session["start_time"], session["end_time"] or session["start_time"],
                    ),
                )

            conn.execute(
                """INSERT INTO work_item_sources(
                       extraction_key, work_item_id, session_id, segment_index,
                       item_index, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(extraction_key) DO UPDATE SET
                       work_item_id=excluded.work_item_id,
                       updated_at=datetime('now')""",
                (extraction_key, work_item_id, session_id, segment_index, item_index),
            )
            _attach_evidence(conn, work_item_id, session_id, item)
            _attach_entities(conn, work_item_id, item["entities"])
            _link_similar_items(conn, work_item_id)
            created_or_updated.append(work_item_id)
    return list(dict.fromkeys(created_or_updated))


def apply_session_analysis(
    session_id: str,
    analysis: dict,
    segment_index: int,
    prompt_version: str,
    model: str,
    auto_approve_threshold: float = 0.92,
    extraction_scope: str = "",
    expected_source_hash: Optional[str] = None,
) -> list[str]:
    """Upsert one structured segment result and return canonical item ids."""
    with get_db(busy_timeout_ms=180_000) as conn:
        if expected_source_hash:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                """SELECT status,source_hash FROM enrichment_jobs
                   WHERE session_id=?""",
                (session_id,),
            ).fetchone()
            current_hash = compute_session_hash(conn, session_id)
            if (
                not job
                or job["status"] != "running"
                or job["source_hash"] != expected_source_hash
                or current_hash != expected_source_hash
            ):
                raise StaleSessionAnalysisError(
                    "会话在整理期间发生变化，已丢弃过期分析结果"
                )
        return _apply_session_analysis_conn(
            conn,
            session_id,
            analysis,
            segment_index,
            prompt_version,
            model,
            auto_approve_threshold,
            extraction_scope,
        )


def apply_session_analyses(
    session_id: str,
    analyses: list[tuple[int, dict, str]],
    prompt_version: str,
    auto_approve_threshold: float,
    extraction_scope: str,
    expected_source_hash: str,
    require_running_job: bool = True,
) -> list[str]:
    """Atomically apply a complete analyzed batch for one immutable snapshot."""
    with get_db(busy_timeout_ms=180_000) as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            "SELECT status,source_hash FROM enrichment_jobs WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if (
            not job
            or (require_running_job and job["status"] != "running")
            or job["status"] == "skipped"
            or job["source_hash"] != expected_source_hash
            or compute_session_hash(conn, session_id) != expected_source_hash
        ):
            raise StaleSessionAnalysisError(
                "会话在整理期间发生变化，已丢弃过期分析结果"
            )
        item_ids = []
        for segment_index, analysis, model in analyses:
            item_ids.extend(_apply_session_analysis_conn(
                conn,
                session_id,
                analysis,
                segment_index,
                prompt_version,
                model,
                auto_approve_threshold,
                extraction_scope,
            ))
        return list(dict.fromkeys(item_ids))


def _attach_evidence(
    conn: sqlite3.Connection, work_item_id: str, session_id: str, item: dict
) -> None:
    ids = []
    for value in item.get("evidence_message_ids") or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not ids:
        row = conn.execute(
            """SELECT id FROM messages WHERE session_id=? AND role IN ('user','assistant')
               ORDER BY CASE role WHEN 'user' THEN 0 ELSE 1 END, id LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row:
            ids = [row["id"]]
    for message_id in ids[:12]:
        message = conn.execute(
            """SELECT id, content FROM messages WHERE id=? AND session_id=?""",
            (message_id, session_id),
        ).fetchone()
        if not message:
            continue
        excerpt = (message["content"] or "").strip()
        if len(excerpt) > 1200:
            excerpt = excerpt[:1200] + "…"
        evidence_key = f"{work_item_id}:{session_id}:{message_id}:support"
        conn.execute(
            """INSERT OR IGNORE INTO work_item_evidence(
                   evidence_key, work_item_id, session_id, message_id,
                   evidence_type, excerpt)
               VALUES (?, ?, ?, ?, 'support', ?)""",
            (evidence_key, work_item_id, session_id, message_id, excerpt),
        )


def _attach_entities(
    conn: sqlite3.Connection, work_item_id: str, entities: Iterable[dict]
) -> None:
    for raw in entities or []:
        if not isinstance(raw, dict):
            continue
        entity_type = str(raw.get("type") or "topic").strip().casefold()[:60]
        name = str(raw.get("name") or "").strip()[:300]
        normalized = _normalize_key(name)
        if not normalized:
            continue
        row = conn.execute(
            "SELECT id FROM entities WHERE entity_type=? AND normalized_name=?",
            (entity_type, normalized),
        ).fetchone()
        entity_id = row["id"] if row else _slug(f"{entity_type}-{name}", "ent")
        conn.execute(
            """INSERT OR IGNORE INTO entities(
                   id, entity_type, canonical_name, normalized_name)
               VALUES (?, ?, ?, ?)""",
            (entity_id, entity_type, name, normalized),
        )
        conn.execute(
            "INSERT OR IGNORE INTO work_item_entities(work_item_id, entity_id) VALUES (?, ?)",
            (work_item_id, entity_id),
        )


def _link_similar_items(conn: sqlite3.Connection, work_item_id: str) -> None:
    """Create conservative, explainable similarity edges without embeddings."""
    current = conn.execute(
        "SELECT keywords_json,category FROM work_items WHERE id=?", (work_item_id,)
    ).fetchone()
    if not current:
        return
    current_keywords = {
        _normalize_key(value) for value in _json(current["keywords_json"], [])
        if _normalize_key(value)
    }
    current_entities = {
        row[0] for row in conn.execute(
            "SELECT entity_id FROM work_item_entities WHERE work_item_id=?",
            (work_item_id,),
        )
    }
    entity_overlap: dict[str, int] = {}
    if current_entities:
        placeholders = ",".join("?" for _ in current_entities)
        entity_overlap = {
            row["work_item_id"]: row["overlap"]
            for row in conn.execute(
                f"""SELECT work_item_id,COUNT(*) AS overlap
                    FROM work_item_entities
                    WHERE entity_id IN ({placeholders}) AND work_item_id!=?
                    GROUP BY work_item_id""",
                [*current_entities, work_item_id],
            ).fetchall()
        }
    candidates = conn.execute(
        """SELECT id,keywords_json,category FROM work_items
           WHERE id!=? AND merged_into_id IS NULL
           ORDER BY updated_at DESC LIMIT 2000""",
        (work_item_id,),
    ).fetchall()
    for candidate in candidates:
        other_keywords = {
            _normalize_key(value) for value in _json(candidate["keywords_json"], [])
            if _normalize_key(value)
        }
        common_keywords = current_keywords & other_keywords
        common_entity_count = entity_overlap.get(candidate["id"], 0)
        if not common_entity_count and len(common_keywords) < 3:
            continue
        union_size = len(current_keywords | other_keywords) or 1
        score = min(
            0.99,
            len(common_keywords) / union_size
            + min(0.45, common_entity_count * 0.22)
            + (0.08 if current["category"] == candidate["category"] else 0),
        )
        if score < 0.35:
            continue
        from_id, to_id = sorted((work_item_id, candidate["id"]))
        conn.execute(
            """INSERT OR IGNORE INTO work_item_relations(
                   from_item_id,to_item_id,relation_type,confidence,ai_generated)
               VALUES (?,?,'similar',?,1)""",
            (from_id, to_id, score),
        )


def _item_dict(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    item["next_steps"] = _json(item.pop("next_steps_json", None), [])
    item["keywords"] = _json(item.pop("keywords_json", None), [])
    item["metadata"] = _json(item.pop("metadata_json", None), {})
    item["ai_generated"] = bool(item.get("ai_generated"))
    return item


def _candidate_rows(
    conn: sqlite3.Connection,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    review_state: Optional[str] = None,
    source: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    concepts: Optional[list[dict]] = None,
) -> list[sqlite3.Row]:
    clauses = ["w.merged_into_id IS NULL"]
    params: list[Any] = []
    for column, value in (
        ("w.kind", kind),
        ("w.project_id", project_id), ("w.review_state", review_state),
    ):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if status == "active":
        clauses.append("w.status IN ('planned','open','in_progress','blocked')")
    elif status:
        clauses.append("w.status=?")
        params.append(status)
    if date_from:
        clauses.append("COALESCE(w.source_end_time,w.source_start_time,w.updated_at)>=?")
        params.append(date_from)
    if date_to:
        clauses.append("COALESCE(w.source_start_time,w.source_end_time,w.updated_at)<=?")
        params.append(date_to)
    if source:
        clauses.append(
            "EXISTS (SELECT 1 FROM work_item_evidence ev JOIN sessions ss "
            "ON ss.id=ev.session_id WHERE ev.work_item_id=w.id AND ss.source=?)"
        )
        params.append(source)
    indexed_variants = []
    for concept in concepts or []:
        for variant in concept.get("variants") or []:
            variant = str(variant).strip()
            if (
                len(variant) >= 3
                and variant.casefold() not in {value.casefold() for value in indexed_variants}
                and len(indexed_variants) < 12
            ):
                indexed_variants.append(variant)
    all_concepts_indexable = bool(concepts) and all(
        any(len(str(variant).strip()) >= 3 for variant in concept.get("variants") or [])
        for concept in concepts or []
    )
    if indexed_variants and all_concepts_indexable:
        fts_columns = (
            "title", "dedupe_key", "project_name", "category", "summary", "problem",
            "root_cause", "solution", "outcome", "next_steps_json", "keywords_text",
        )
        fts_conditions = []
        fts_params = []
        for variant in indexed_variants:
            for column in fts_columns:
                fts_conditions.append(f"{column} LIKE ?")
                fts_params.append(f"%{variant}%")
        parent_conditions = []
        parent_params = []
        for variant in indexed_variants:
            parent_conditions.extend([
                "mtp.title LIKE ?", "mtp.objective LIKE ?",
            ])
            parent_params.extend([f"%{variant}%"] * 2)
        clauses.append(
            "w.rowid IN (SELECT rowid FROM work_items_fts WHERE "
            + " OR ".join(fts_conditions)
            + " UNION SELECT wp.rowid FROM work_items wp "
              "JOIN major_tasks mtp ON mtp.id=wp.major_task_id WHERE "
            + " OR ".join(parent_conditions) + ")"
        )
        params.extend(fts_params)
        params.extend(parent_params)
    return conn.execute(
        f"""SELECT w.*,
                   mt.title AS major_task_title,
                   mt.objective AS major_task_summary,
                   (SELECT COUNT(*) FROM work_item_evidence e
                    WHERE e.work_item_id=w.id) AS evidence_count,
                   (SELECT COUNT(DISTINCT session_id) FROM work_item_evidence e
                    WHERE e.work_item_id=w.id) AS session_count,
                   (SELECT GROUP_CONCAT(DISTINCT ss.source)
                    FROM work_item_evidence e JOIN sessions ss ON ss.id=e.session_id
                    WHERE e.work_item_id=w.id) AS sources
            FROM work_items w
            LEFT JOIN major_tasks mt ON mt.id=w.major_task_id
            WHERE {' AND '.join(clauses)}""",
        params,
    ).fetchall()


def _search_concepts(query: str, extra_terms: Optional[list[str]] = None) -> list[dict]:
    from app.database import _parse_search_concepts

    concepts = _parse_search_concepts(query or "")
    seen = {entry["label"].casefold() for entry in concepts}
    for term in extra_terms or []:
        term = str(term).strip()
        if len(term) >= 2 and term.casefold() not in seen:
            concepts.append({"label": term, "variants": [term]})
            seen.add(term.casefold())
    return concepts[:30]


def search_work_items(
    query: str = "",
    *,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    review_state: Optional[str] = None,
    source: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    extra_terms: Optional[list[str]] = None,
    parent_ids_out: Optional[set[str]] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Hybrid structured/full-text scorer returning one row per work item."""
    query = (query or "").strip()
    concepts = _search_concepts(query, extra_terms)
    with get_db() as conn:
        rows = _candidate_rows(
            conn, kind, status, project_id, review_state, source, date_from, date_to,
            concepts,
        )
        ranked: list[tuple[float, dict]] = []
        now = datetime.now(timezone.utc)
        for row in rows:
            item = _item_dict(row)
            fields = {
                "title": item.get("title") or "",
                "dedupe": item.get("dedupe_key") or "",
                "project": item.get("project_name") or "",
                "keywords": " ".join(item.get("keywords") or []),
                "problem": item.get("problem") or "",
                "root_cause": item.get("root_cause") or "",
                "solution": item.get("solution") or "",
                "outcome": item.get("outcome") or "",
                "summary": item.get("summary") or "",
                "next_steps": " ".join(item.get("next_steps") or []),
                "category": item.get("category") or "",
                "major_task": " ".join(filter(None, (
                    item.get("major_task_title"), item.get("major_task_summary")
                ))),
            }
            lowered = {key: value.casefold() for key, value in fields.items()}
            query_folded = query.casefold()
            meta_text = f"{lowered['title']} {lowered['category']}"
            is_meta_search_result = bool(
                query
                and not any(
                    marker in query_folded
                    for marker in ("搜索", "查找", "找不到", "search")
                )
                and any(
                    marker in meta_text
                    for marker in ("搜索", "查找", "找不到", "search")
                )
            )
            matched: list[str] = []
            score = 0.0
            reasons: list[str] = []
            for concept in concepts:
                best = 0
                for variant in concept.get("variants") or [concept["label"]]:
                    needle = str(variant).casefold()
                    if not needle:
                        continue
                    for field, weight in (
                        ("title", 24), ("dedupe", 20), ("keywords", 16),
                        ("problem", 12), ("root_cause", 12), ("solution", 11),
                        ("outcome", 10), ("summary", 8), ("project", 7),
                        ("major_task", 18), ("next_steps", 5), ("category", 4),
                    ):
                        if needle in lowered[field]:
                            best = max(best, weight)
                if best:
                    matched.append(concept["label"])
                    score += best
            if (
                query
                and query_folded in lowered["title"]
                and not is_meta_search_result
            ):
                score += 80
                reasons.append("标题完整命中")
            coverage = len(matched)
            if query and not coverage:
                continue
            if coverage:
                score += coverage * coverage * 5
                reasons.append(f"命中 {coverage}/{max(1, len(concepts))} 个概念")
            # A later conversation about fixing search often repeats the full
            # historical query. Keep it discoverable, but do not let that
            # meta-work outrank the task the user was actually trying to find.
            if is_meta_search_result:
                score -= 30
                reasons.append("搜索讨论降权")
            if item.get("review_state") in ("approved", "edited"):
                score += 5
            end_time = _parse_time(item.get("source_end_time") or item.get("updated_at"))
            if end_time:
                if end_time.tzinfo is None:
                    end_time = end_time.replace(tzinfo=timezone.utc)
                days = max(0, (now - end_time).days)
                score += max(0, 8 - math.log2(days + 1))
            item["matched_terms"] = matched
            item["term_count"] = len(concepts)
            item["term_coverage"] = coverage
            item["relevance_score"] = round(score, 3)
            item["why_matched"] = reasons
            item["sources"] = (item.get("sources") or "").split(",") if item.get("sources") else []
            if parent_ids_out is not None and item.get("major_task_id"):
                parent_ids_out.add(item["major_task_id"])
            ranked.append((score, item))
        ranked.sort(
            key=lambda pair: (
                pair[0] if query else 0,
                pair[1].get("source_end_time") or pair[1].get("updated_at") or "",
            ),
            reverse=True,
        )
        total = len(ranked)
        return [item for _, item in ranked[offset:offset + limit]], total


def _bullet(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^(?:[-*•]|\d+[.)、])\s*", "", text)
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def _unique_bullets(values: Iterable[Any], limit: int = _SUMMARY_BULLET_LIMIT) -> list[str]:
    results: list[str] = []
    seen = set()
    for value in values:
        if isinstance(value, list):
            candidates = value
        else:
            candidates = str(value or "").splitlines()
        for candidate in candidates:
            text = _bullet(candidate)
            key = re.sub(r"\W+", "", text.casefold())
            if not text or not key or key in seen:
                continue
            seen.add(key)
            results.append(text)
            if len(results) >= limit:
                return results
    return results


def _major_task_summary_sections(
    major_task: dict,
    items: list[dict],
    *,
    knowledge_only: bool = False,
) -> list[dict]:
    """Build a concise, evidence-derived outline without another model call."""
    newest = sorted(
        items,
        key=lambda item: item.get("source_end_time") or item.get("updated_at") or "",
        reverse=True,
    )
    if knowledge_only:
        problem_items = [item for item in newest if item.get("problem") or item.get("root_cause")]
        solution_items = [item for item in newest if item.get("solution")]
        conclusion_items = [
            item for item in newest
            if item.get("kind") in ("knowledge", "solution", "decision")
        ]
        sections = [
            (
                "problem", "问题与根因",
                _unique_bullets(
                    value
                    for item in problem_items
                    for value in (item.get("problem"), item.get("root_cause"))
                ),
            ),
            (
                "solution", "可复用方案",
                _unique_bullets(item.get("solution") for item in solution_items),
            ),
            (
                "conclusion", "经验与结论",
                _unique_bullets(
                    value
                    for item in conclusion_items
                    for value in (item.get("outcome"), item.get("summary"))
                ),
            ),
            (
                "follow_up", "待验证 / 下一步",
                _unique_bullets(
                    item.get("next_steps") or [] for item in newest
                    if item.get("status") in ACTIVE_STATUSES
                ),
            ),
        ]
    else:
        active = [item for item in newest if item.get("status") in ACTIVE_STATUSES]
        finished = [item for item in newest if item.get("status") == "done"]
        conclusions = [
            item for item in newest
            if item.get("kind") in ("decision", "solution", "knowledge")
        ]
        blocked = [item for item in active if item.get("status") == "blocked"]
        goal_values: list[Any] = [major_task.get("objective")]
        goal_values.extend(
            item.get("problem") or item.get("summary") or item.get("title")
            for item in (active or newest)
        )
        progress_values = []
        for item in [*active[:3], *finished[:2]]:
            detail = item.get("outcome") or item.get("summary") or ""
            progress_values.append(
                f"{item.get('title')}{'：' + detail if detail and detail != item.get('title') else ''}"
            )
        sections = [
            ("goal", "目标与范围", _unique_bullets(goal_values, 3)),
            ("progress", "当前进展", _unique_bullets(progress_values)),
            (
                "conclusion", "关键结论",
                _unique_bullets(
                    value
                    for item in conclusions
                    for value in (
                        item.get("outcome"), item.get("solution"), item.get("summary")
                    )
                ),
            ),
            (
                "blocker", "阻塞与风险",
                _unique_bullets(
                    item.get("problem") or item.get("summary") or item.get("title")
                    for item in blocked
                ),
            ),
            (
                "next", "下一步",
                _unique_bullets(
                    [
                        step
                        for item in active
                        for step in (item.get("next_steps") or [])
                    ]
                    or [item.get("title") for item in active]
                ),
            ),
        ]
    result = [
        {"key": key, "label": label, "items": bullets}
        for key, label, bullets in sections if bullets
    ]
    if not result and newest:
        result.append({
            "key": "items",
            "label": "包含事项",
            "items": _unique_bullets(item.get("title") for item in newest),
        })
    return result


def _major_task_rows(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    kind: Optional[str] = None,
    child_kinds: Optional[tuple[str, ...]] = None,
    major_task_id: Optional[str] = None,
) -> list[dict]:
    clauses = [
        "w.merged_into_id IS NULL",
        "w.review_state!='dismissed'",
    ]
    params: list[Any] = []
    if project_id:
        clauses.append("mt.project_id=?")
        params.append(project_id)
    if major_task_id:
        clauses.append("mt.id=?")
        params.append(major_task_id)
    group_filters = []
    group_params: list[Any] = []
    if status == "active":
        group_filters.append(
            "EXISTS (SELECT 1 FROM work_items wf WHERE wf.major_task_id=mt.id "
            "AND wf.merged_into_id IS NULL AND wf.review_state!='dismissed' "
            "AND wf.status IN ('planned','open','in_progress','blocked'))"
        )
    elif status:
        group_filters.append(
            "EXISTS (SELECT 1 FROM work_items wf WHERE wf.major_task_id=mt.id "
            "AND wf.merged_into_id IS NULL AND wf.review_state!='dismissed' "
            "AND wf.status=?)"
        )
        group_params.append(status)
    effective_kinds = child_kinds or ((kind,) if kind else ())
    if effective_kinds:
        placeholders = ",".join("?" for _ in effective_kinds)
        group_filters.append(
            "EXISTS (SELECT 1 FROM work_items wk WHERE wk.major_task_id=mt.id "
            "AND wk.merged_into_id IS NULL AND wk.review_state!='dismissed' "
            f"AND wk.kind IN ({placeholders}))"
        )
        group_params.extend(effective_kinds)
    where = " AND ".join([*clauses, *group_filters])
    rows = conn.execute(
        f"""SELECT mt.*,
                   COUNT(w.id) AS item_count,
                   SUM(CASE WHEN w.status='done' THEN 1 ELSE 0 END) AS done_count,
                   SUM(CASE WHEN w.status IN ('planned','open','in_progress','blocked')
                            THEN 1 ELSE 0 END) AS active_count,
                   SUM(CASE WHEN w.status='blocked' THEN 1 ELSE 0 END) AS blocked_count,
                   SUM(CASE WHEN w.kind='task' THEN 1 ELSE 0 END) AS task_count,
                   SUM(CASE WHEN w.kind='decision' THEN 1 ELSE 0 END) AS decision_count,
                   SUM(CASE WHEN w.kind='solution' THEN 1 ELSE 0 END) AS solution_count,
                   SUM(CASE WHEN w.kind='knowledge' THEN 1 ELSE 0 END) AS knowledge_count,
                   COUNT(DISTINCT w.project_name) AS project_count,
                   MAX(COALESCE(w.source_end_time,w.updated_at)) AS last_activity
            FROM major_tasks mt JOIN work_items w ON w.major_task_id=mt.id
            WHERE {where}
            GROUP BY mt.id
            ORDER BY (SUM(CASE WHEN w.status IN ('planned','open','in_progress','blocked')
                               THEN 1 ELSE 0 END)>0) DESC,
                     last_activity DESC,mt.title""",
        [*params, *group_params],
    ).fetchall()
    return [dict(row) for row in rows]


def _major_status(row: dict) -> str:
    if int(row.get("blocked_count") or 0) > 0:
        return "blocked"
    if int(row.get("active_count") or 0) > 0:
        return "in_progress"
    return "done" if int(row.get("done_count") or 0) > 0 else "open"


def _sample_major_task_items(
    conn: sqlite3.Connection,
    major_task_ids: list[str],
    *,
    child_kinds: Optional[tuple[str, ...]] = None,
    per_group: int = 40,
) -> dict[str, list[dict]]:
    if not major_task_ids:
        return {}
    placeholders = ",".join("?" for _ in major_task_ids)
    params: list[Any] = [*major_task_ids]
    kind_filter = ""
    if child_kinds:
        kind_placeholders = ",".join("?" for _ in child_kinds)
        kind_filter = f" AND kind IN ({kind_placeholders})"
        params.extend(child_kinds)
    params.append(per_group)
    rows = conn.execute(
        f"""SELECT * FROM (
                SELECT w.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY major_task_id
                           ORDER BY CASE status
                               WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1
                               WHEN 'open' THEN 2 WHEN 'planned' THEN 3 ELSE 4 END,
                               COALESCE(source_end_time,updated_at) DESC,id
                       ) AS group_row
                FROM work_items w
                WHERE major_task_id IN ({placeholders})
                  AND merged_into_id IS NULL AND review_state!='dismissed'
                  {kind_filter}
            ) ranked WHERE group_row<=?
            ORDER BY major_task_id,group_row""",
        params,
    ).fetchall()
    grouped: dict[str, list[dict]] = {item_id: [] for item_id in major_task_ids}
    for row in rows:
        item = _item_dict(row)
        item.pop("group_row", None)
        grouped.setdefault(item.get("major_task_id"), []).append(item)
    return grouped


def list_major_tasks(
    query: str = "",
    *,
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    kind: Optional[str] = None,
    child_kinds: Optional[tuple[str, ...]] = None,
    limit: int = 30,
    offset: int = 0,
    knowledge_only: bool = False,
    matched_parent_ids: Optional[set[str]] = None,
) -> tuple[list[dict], int]:
    """List the parent-level work units with live, bullet-based rollups."""
    query = (query or "").strip()
    resolved_parent_ids: set[str] = set(matched_parent_ids or ())
    if query and matched_parent_ids is None:
        search_kinds = child_kinds or ((kind,) if kind else (None,))
        for search_kind in search_kinds:
            matched_items, _ = search_work_items(
                query,
                status=status,
                project_id=project_id,
                kind=search_kind,
                limit=100_000,
            )
            resolved_parent_ids.update(
                item.get("major_task_id") for item in matched_items
                if item.get("major_task_id")
            )
    with get_db() as conn:
        rows = _major_task_rows(
            conn,
            status=status,
            project_id=project_id,
            kind=kind,
            child_kinds=child_kinds,
        )
        if query:
            folded = query.casefold()
            rows = [
                row for row in rows
                if row["id"] in resolved_parent_ids
                or folded in (row.get("title") or "").casefold()
                or folded in (row.get("objective") or "").casefold()
            ]
        total = len(rows)
        page = rows[offset:offset + limit]
        ids = [row["id"] for row in page]
        samples = _sample_major_task_items(
            conn,
            ids,
            child_kinds=child_kinds if knowledge_only else None,
        )
        session_counts = {
            row["major_task_id"]: int(row["session_count"] or 0)
            for row in conn.execute(
                f"""SELECT w.major_task_id,COUNT(DISTINCT e.session_id) AS session_count
                    FROM work_items w JOIN work_item_evidence e ON e.work_item_id=w.id
                    WHERE w.major_task_id IN ({','.join('?' for _ in ids)})
                    GROUP BY w.major_task_id""",
                ids,
            ).fetchall()
        } if ids else {}
    result = []
    for row in page:
        children = samples.get(row["id"], [])
        row["status"] = _major_status(row)
        row["progress_percent"] = round(
            100 * int(row.get("done_count") or 0) / max(1, int(row.get("item_count") or 0))
        )
        row["session_count"] = session_counts.get(row["id"], 0)
        row["summary_sections"] = _major_task_summary_sections(
            row, children, knowledge_only=knowledge_only
        )
        row["items_preview"] = children[:6]
        result.append(row)
    return result, total


def get_major_task(
    major_task_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> Optional[dict]:
    with get_db() as conn:
        rows = _major_task_rows(conn, major_task_id=major_task_id)
        if not rows:
            return None
        major_task = rows[0]
        item_rows = conn.execute(
            """SELECT w.* FROM work_items w
               WHERE w.major_task_id=? AND w.merged_into_id IS NULL
                 AND w.review_state!='dismissed'
               ORDER BY CASE w.status
                   WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1
                   WHEN 'open' THEN 2 WHEN 'planned' THEN 3 ELSE 4 END,
                   COALESCE(w.source_end_time,w.updated_at) DESC,w.id
               LIMIT ? OFFSET ?""",
            (major_task_id, limit, offset),
        ).fetchall()
        summary_rows = conn.execute(
            """SELECT w.* FROM work_items w
               WHERE w.major_task_id=? AND w.merged_into_id IS NULL
                 AND w.review_state!='dismissed'
               ORDER BY COALESCE(w.source_end_time,w.updated_at) DESC
               LIMIT 200""",
            (major_task_id,),
        ).fetchall()
    items = [_item_dict(row) for row in item_rows]
    summary_items = [_item_dict(row) for row in summary_rows]
    major_task["status"] = _major_status(major_task)
    major_task["progress_percent"] = round(
        100 * int(major_task.get("done_count") or 0)
        / max(1, int(major_task.get("item_count") or 0))
    )
    major_task["summary_sections"] = _major_task_summary_sections(
        major_task, summary_items
    )
    major_task["items"] = items
    major_task["offset"] = offset
    major_task["limit"] = limit
    major_task["has_more"] = offset + len(items) < int(major_task["item_count"] or 0)
    return major_task


def get_work_item(item_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            """SELECT w.*,
                      mt.title AS major_task_title,
                      mt.objective AS major_task_summary,
                      (SELECT COUNT(*) FROM work_item_evidence e
                       WHERE e.work_item_id=w.id) AS evidence_count,
                      (SELECT COUNT(DISTINCT session_id) FROM work_item_evidence e
                       WHERE e.work_item_id=w.id) AS session_count
               FROM work_items w
               LEFT JOIN major_tasks mt ON mt.id=w.major_task_id
               WHERE w.id=?""",
            (item_id,),
        ).fetchone()
        if not row:
            return None
        item = _item_dict(row)
        evidence = conn.execute(
            """SELECT e.*, s.title AS session_title, s.source, s.server_name,
                      s.cwd, s.start_time, s.end_time
               FROM work_item_evidence e JOIN sessions s ON s.id=e.session_id
               WHERE e.work_item_id=?
               ORDER BY COALESCE(s.end_time,s.start_time) DESC, e.id""",
            (item_id,),
        ).fetchall()
        item["evidence"] = []
        for evidence_row in evidence:
            value = dict(evidence_row)
            value["resume_command"] = build_resume_command(
                value.get("source"), value.get("session_id"), value.get("cwd")
            )
            item["evidence"].append(value)
        item["entities"] = [
            dict(value) for value in conn.execute(
                """SELECT en.* FROM entities en JOIN work_item_entities we
                   ON we.entity_id=en.id WHERE we.work_item_id=?
                   ORDER BY en.entity_type,en.canonical_name""",
                (item_id,),
            ).fetchall()
        ]
        item["relations"] = [
            dict(value) for value in conn.execute(
                """SELECT r.*,
                          CASE WHEN r.from_item_id=? THEN r.to_item_id
                               ELSE r.from_item_id END AS related_item_id,
                          w.title AS related_title
                   FROM work_item_relations r JOIN work_items w ON w.id=
                       CASE WHEN r.from_item_id=? THEN r.to_item_id
                            ELSE r.from_item_id END
                   WHERE r.from_item_id=? OR r.to_item_id=?""",
                (item_id, item_id, item_id, item_id),
            ).fetchall()
        ]
        return item


EDITABLE_FIELDS = {
    "kind", "title", "project_name", "category", "status", "priority",
    "summary", "problem", "root_cause", "solution", "outcome",
    "next_steps", "keywords", "review_state", "major_task_id",
    "major_task_title",
}


def create_work_item(payload: dict) -> dict:
    item = _clean_item(payload)
    with get_db() as conn:
        work_item_id = f"wi-{uuid.uuid4().hex}"
        project_id = _project_id(conn, item["project"])
        major_task_id = item.get("major_task_id") or ""
        if major_task_id and not conn.execute(
            "SELECT 1 FROM major_tasks WHERE id=?", (major_task_id,)
        ).fetchone():
            major_task_id = ""
        if not major_task_id:
            major_task_id = _ensure_major_task(
                conn, item, project_id=project_id
            )
        conn.execute(
            """INSERT INTO work_items(
                   id,dedupe_key,kind,title,project_id,project_name,major_task_id,category,
                   status,priority,summary,problem,root_cause,solution,outcome,
                   next_steps_json,keywords_json,keywords_text,confidence,
                   review_state,ai_generated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'edited',0)""",
            (
                work_item_id, item["dedupe_key"], item["kind"], item["title"],
                project_id, item["project"], major_task_id, item["category"], item["status"],
                item["priority"], item["summary"], item["problem"],
                item["root_cause"], item["solution"], item["outcome"],
                _dump(item["next_steps"]), _dump(item["keywords"]),
                " ".join(item["keywords"]),
            ),
        )
    return get_work_item(work_item_id) or {}


def update_work_item(item_id: str, payload: dict, action: str = "edit") -> Optional[dict]:
    updates = {key: value for key, value in payload.items() if key in EDITABLE_FIELDS}
    if not updates:
        return get_work_item(item_id)
    with get_db() as conn:
        before_row = conn.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
        if not before_row:
            return None
        requested_major_title = str(updates.pop("major_task_title", "") or "").strip()
        requested_major_id = updates.get("major_task_id")
        if requested_major_title:
            target_project = str(
                updates.get("project_name", before_row["project_name"] or "") or ""
            ).strip()
            parent_seed = {
                "project": target_project,
                "title": before_row["title"],
                "major_task_key": requested_major_title,
                "major_task_title": requested_major_title,
                "major_task_summary": "",
            }
            requested_major_id = _ensure_major_task(conn, parent_seed)
            conn.execute(
                "UPDATE major_tasks SET title_source='manual' WHERE id=?",
                (requested_major_id,),
            )
            updates["major_task_id"] = requested_major_id
        elif requested_major_id is not None:
            requested_major_id = str(requested_major_id or "").strip()
            if not requested_major_id or not conn.execute(
                "SELECT 1 FROM major_tasks WHERE id=?", (requested_major_id,)
            ).fetchone():
                updates.pop("major_task_id", None)
        assignments = []
        params: list[Any] = []
        for key, value in updates.items():
            if key == "project_name":
                assignments.extend(["project_name=?", "project_id=?"])
                params.extend([str(value or "").strip(), _project_id(conn, value)])
            elif key in ("next_steps", "keywords"):
                column = JSON_FIELDS[key]
                cleaned = [str(entry).strip() for entry in (value or []) if str(entry).strip()]
                assignments.append(f"{column}=?")
                params.append(_dump(cleaned))
                if key == "keywords":
                    assignments.append("keywords_text=?")
                    params.append(" ".join(cleaned))
            elif key == "review_state" and value not in REVIEW_STATES:
                continue
            elif key == "kind" and value not in WORK_KINDS:
                continue
            elif key == "status" and value not in WORK_STATUSES:
                continue
            else:
                assignments.append(f"{key}=?")
                params.append(value)
        if action == "edit" and "review_state" not in updates:
            assignments.append("review_state='edited'")
        assignments.append("updated_at=datetime('now')")
        params.append(item_id)
        conn.execute(
            f"UPDATE work_items SET {', '.join(assignments)} WHERE id=?", params
        )
        after_row = conn.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
        conn.execute(
            """INSERT INTO work_feedback(work_item_id,action,before_json,after_json)
               VALUES (?,?,?,?)""",
            (item_id, action, _dump(dict(before_row)), _dump(dict(after_row))),
        )
    return get_work_item(item_id)


def approve_work_items(
    item_ids: Optional[Iterable[str]] = None,
    *,
    all_pending: bool = False,
    expected_total: Optional[int] = None,
) -> dict:
    """Approve pending work items in one audited transaction.

    ``expected_total`` protects an "all pending" selection from silently
    including items that arrived after the user made the selection.
    """
    requested_ids = list(dict.fromkeys(
        str(value).strip() for value in (item_ids or []) if str(value).strip()
    ))
    if not all_pending and not requested_ids:
        return {
            "updated_count": 0,
            "skipped_count": 0,
            "remaining": get_work_stats()["inbox"],
        }

    with get_db() as conn:
        if all_pending:
            rows = conn.execute(
                "SELECT * FROM work_items WHERE review_state='pending' ORDER BY id"
            ).fetchall()
            if expected_total is not None and len(rows) != expected_total:
                return {
                    "conflict": True,
                    "expected_total": expected_total,
                    "actual_total": len(rows),
                }
            skipped_count = 0
        else:
            placeholders = ",".join("?" for _ in requested_ids)
            rows = conn.execute(
                f"""SELECT * FROM work_items
                    WHERE review_state='pending' AND id IN ({placeholders})
                    ORDER BY id""",
                requested_ids,
            ).fetchall()
            skipped_count = len(requested_ids) - len(rows)

        if rows:
            approved_at = conn.execute("SELECT datetime('now')").fetchone()[0]
            approved_ids = [row["id"] for row in rows]
            conn.executemany(
                """UPDATE work_items
                   SET review_state='approved',updated_at=?
                   WHERE id=? AND review_state='pending'""",
                ((approved_at, item_id) for item_id in approved_ids),
            )
            feedback_rows = []
            for row in rows:
                before = dict(row)
                after = {**before, "review_state": "approved", "updated_at": approved_at}
                feedback_rows.append((row["id"], "approve", _dump(before), _dump(after)))
            conn.executemany(
                """INSERT INTO work_feedback(
                       work_item_id,action,before_json,after_json)
                   VALUES (?,?,?,?)""",
                feedback_rows,
            )

        remaining = conn.execute(
            "SELECT COUNT(*) FROM work_items WHERE review_state='pending'"
        ).fetchone()[0]
        return {
            "updated_count": len(rows),
            "skipped_count": skipped_count,
            "remaining": remaining,
        }


def merge_work_items(target_id: str, source_ids: list[str]) -> Optional[dict]:
    source_ids = [value for value in dict.fromkeys(source_ids) if value != target_id]
    if not source_ids:
        return get_work_item(target_id)
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM work_items WHERE id=?", (target_id,)).fetchone():
            return None
        for source_id in source_ids:
            if not conn.execute("SELECT 1 FROM work_items WHERE id=?", (source_id,)).fetchone():
                continue
            conn.execute(
                "UPDATE OR REPLACE work_item_sources SET work_item_id=? WHERE work_item_id=?",
                (target_id, source_id),
            )
            for table, columns in (
                ("work_item_evidence", "evidence_key,session_id,message_id,evidence_type,excerpt"),
            ):
                rows = conn.execute(
                    f"SELECT {columns} FROM {table} WHERE work_item_id=?", (source_id,)
                ).fetchall()
                for row in rows:
                    evidence_key = f"{target_id}:{row['session_id']}:{row['message_id']}:{row['evidence_type']}"
                    conn.execute(
                        """INSERT OR IGNORE INTO work_item_evidence(
                               evidence_key,work_item_id,session_id,message_id,
                               evidence_type,excerpt) VALUES (?,?,?,?,?,?)""",
                        (evidence_key, target_id, row["session_id"], row["message_id"], row["evidence_type"], row["excerpt"]),
                    )
            conn.execute(
                """INSERT OR IGNORE INTO work_item_entities(work_item_id,entity_id)
                   SELECT ?,entity_id FROM work_item_entities WHERE work_item_id=?""",
                (target_id, source_id),
            )
            conn.execute(
                "UPDATE work_items SET merged_into_id=?, updated_at=datetime('now') WHERE id=?",
                (target_id, source_id),
            )
            conn.execute(
                """INSERT OR IGNORE INTO work_item_relations(
                       from_item_id,to_item_id,relation_type,confidence,ai_generated)
                   VALUES (?,?,'merged_from',1,0)""",
                (target_id, source_id),
            )
        conn.execute(
            "UPDATE work_items SET review_state='edited',updated_at=datetime('now') WHERE id=?",
            (target_id,),
        )
        conn.execute(
            "INSERT INTO work_feedback(work_item_id,action,after_json) VALUES (?,'merge',?)",
            (target_id, _dump({"source_ids": source_ids})),
        )
    return get_work_item(target_id)


def split_work_item(item_id: str, payload: dict, evidence_ids: list[int]) -> Optional[dict]:
    original = get_work_item(item_id)
    if not original:
        return None
    seed = {key: original.get(key) for key in EDITABLE_FIELDS}
    seed.update(payload)
    created = create_work_item(seed)
    new_id = created["id"]
    with get_db() as conn:
        if evidence_ids:
            placeholders = ",".join("?" for _ in evidence_ids)
            rows = conn.execute(
                f"""SELECT * FROM work_item_evidence
                    WHERE work_item_id=? AND id IN ({placeholders})""",
                [item_id, *evidence_ids],
            ).fetchall()
            for row in rows:
                key = f"{new_id}:{row['session_id']}:{row['message_id']}:{row['evidence_type']}"
                conn.execute(
                    """INSERT OR IGNORE INTO work_item_evidence(
                           evidence_key,work_item_id,session_id,message_id,
                           evidence_type,excerpt) VALUES (?,?,?,?,?,?)""",
                    (key, new_id, row["session_id"], row["message_id"], row["evidence_type"], row["excerpt"]),
                )
        conn.execute(
            """INSERT OR IGNORE INTO work_item_relations(
                   from_item_id,to_item_id,relation_type,confidence,ai_generated)
               VALUES (?,?,'split_from',1,0)""",
            (new_id, item_id),
        )
    return get_work_item(new_id)


def list_projects(limit: int = 200) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT p.*,
                      COUNT(w.id) AS item_count,
                      SUM(CASE WHEN w.status='done' THEN 1 ELSE 0 END) AS done_count,
                      SUM(CASE WHEN w.status IN ('planned','open','in_progress','blocked')
                               THEN 1 ELSE 0 END) AS active_count,
                      SUM(CASE WHEN w.status='blocked' THEN 1 ELSE 0 END) AS blocked_count,
                      MAX(COALESCE(w.source_end_time,w.updated_at)) AS last_activity
               FROM projects p LEFT JOIN work_items w
                 ON w.project_id=p.id AND w.merged_into_id IS NULL
               GROUP BY p.id ORDER BY last_activity DESC, p.name LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_project(project_id: str) -> Optional[dict]:
    with get_db() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        return None
    items, total = search_work_items(project_id=project_id, limit=500)
    result = dict(project)
    result["items"] = items
    result["item_count"] = total
    return result


def get_work_stats() -> dict:
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM work_items WHERE merged_into_id IS NULL"
        ).fetchone()[0]
        by_status = conn.execute(
            """SELECT status,COUNT(*) count FROM work_items
               WHERE merged_into_id IS NULL GROUP BY status"""
        ).fetchall()
        by_kind = conn.execute(
            """SELECT kind,COUNT(*) count FROM work_items
               WHERE merged_into_id IS NULL GROUP BY kind"""
        ).fetchall()
        inbox = conn.execute(
            """SELECT COUNT(*) FROM work_items WHERE merged_into_id IS NULL
               AND review_state='pending'"""
        ).fetchone()[0]
        projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        major_tasks = conn.execute(
            """SELECT COUNT(DISTINCT major_task_id) FROM work_items
               WHERE merged_into_id IS NULL AND review_state!='dismissed'
                 AND major_task_id IS NOT NULL"""
        ).fetchone()[0]
        analyzed = conn.execute(
            "SELECT COUNT(*) FROM enrichment_jobs WHERE status='completed'"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM enrichment_jobs WHERE status='pending'"
        ).fetchone()[0]
        return {
            "total_items": total,
            "inbox": inbox,
            "projects": projects,
            "major_tasks": major_tasks,
            "analyzed_sessions": analyzed,
            "pending_sessions": pending,
            "by_status": {row["status"]: row["count"] for row in by_status},
            "by_kind": {row["kind"]: row["count"] for row in by_kind},
        }


_TODAY_ACTIVE_FIELDS = (
    "id", "title", "kind", "status", "summary", "problem", "project_name",
    "source_end_time", "updated_at", "next_steps", "session_count", "sources",
)


def _primary_sessions_for_items(
    conn: sqlite3.Connection, item_ids: list[str]
) -> dict[str, dict]:
    """Return the most recent evidence/source session for each work item."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"""WITH associated AS (
                SELECT work_item_id,session_id FROM work_item_sources
                WHERE work_item_id IN ({placeholders})
                UNION
                SELECT work_item_id,session_id FROM work_item_evidence
                WHERE work_item_id IN ({placeholders})
            ), ranked AS (
                SELECT a.work_item_id,s.id,s.title,s.source,s.cwd,
                       s.start_time,s.end_time,
                       si.summary AS insight_summary,
                       si.project_name AS insight_project_name,
                       ROW_NUMBER() OVER (
                           PARTITION BY a.work_item_id
                           ORDER BY COALESCE(s.end_time,s.start_time,'') DESC,s.id DESC
                       ) AS session_rank
                FROM associated a
                JOIN sessions s ON s.id=a.session_id
                LEFT JOIN session_insights si ON si.session_id=s.id
            )
            SELECT * FROM ranked WHERE session_rank=1""",
        [*item_ids, *item_ids],
    ).fetchall()
    result = {}
    for row in rows:
        value = dict(row)
        work_item_id = value.pop("work_item_id")
        value.pop("session_rank", None)
        result[work_item_id] = value
    return result


def _group_active_items_by_session(
    items: list[dict], sessions_by_item: dict[str, dict]
) -> list[dict]:
    groups: dict[str, dict] = {}
    for item in items:
        session = sessions_by_item.get(item["id"])
        key = session["id"] if session else "__unlinked__"
        if key not in groups:
            groups[key] = {
                "key": key,
                "session": session,
                "item_ids": [],
            }
        groups[key]["item_ids"].append(item["id"])
    return list(groups.values())


def _session_pipeline_state(job: Optional[dict], has_items: bool) -> dict:
    """Describe the visible lifecycle of one synced session.

    Raw capture and Agent-derived work are intentionally separate.  The
    dashboard can therefore show a session immediately, while making it clear
    whether its task structure is still being produced.
    """
    status = str((job or {}).get("status") or "")
    updated_at = (job or {}).get("updated_at")
    if status == "running":
        return {
            "stage": "understanding",
            "label": "正在理解",
            "detail": (
                "Agent 正在吸收最新消息；已有任务会保留，整理完成后自动更新。"
                if has_items else
                "Agent 正在提炼任务、关键结论和下一步。"
            ),
            "job_status": status,
            "updated_at": updated_at,
            "not_before": (job or {}).get("not_before"),
            "is_error": False,
        }
    if status == "pending":
        not_before = (job or {}).get("not_before")
        parsed_not_before = _parse_time(not_before)
        if parsed_not_before and parsed_not_before.tzinfo is None:
            parsed_not_before = parsed_not_before.replace(tzinfo=timezone.utc)
        waiting_for_settle = bool(
            parsed_not_before and parsed_not_before > datetime.now(timezone.utc)
        )
        if waiting_for_settle:
            detail = (
                "已捕获最新消息，等待会话短暂静默后开始整理，避免反复打断当前工作。"
            )
        else:
            detail = "已进入理解队列，Agent 将自动提炼任务、关键结论和下一步。"
        if has_items:
            detail = "正在吸收这个会话的最新变化；现有任务先保留，完成后自动更新。"
        return {
            "stage": "understanding",
            "label": "正在理解",
            "detail": detail,
            "job_status": status,
            "updated_at": updated_at,
            "not_before": not_before,
            "is_error": False,
        }
    if status == "failed":
        return {
            "stage": "captured",
            "label": "已捕获",
            "detail": "原始会话已安全保存；自动理解暂时受阻，可在“数据源与 Agent”中重试。",
            "job_status": status,
            "updated_at": updated_at,
            "not_before": None,
            "is_error": True,
        }
    if status == "skipped":
        return {
            "stage": "captured",
            "label": "已捕获",
            "detail": "内部审查会话仅保留原始记录，不进入自动整理。",
            "job_status": status,
            "updated_at": updated_at,
            "not_before": None,
            "is_error": False,
        }
    if has_items:
        return {
            "stage": "ready",
            "label": "已形成任务",
            "detail": "任务结构已生成，可以继续查看和推进。",
            "job_status": status or "completed",
            "updated_at": updated_at,
            "not_before": None,
            "is_error": False,
        }
    return {
        "stage": "captured",
        "label": "已捕获",
        "detail": "原始会话已同步，等待进入自动理解流程。",
        "job_status": status or "captured",
        "updated_at": updated_at,
        "not_before": None,
        "is_error": False,
    }


def _enrichment_jobs_for_sessions(
    conn: sqlite3.Connection, session_ids: Iterable[str]
) -> dict[str, dict]:
    ids = list(dict.fromkeys(value for value in session_ids if value))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT session_id,status,priority,reason,source_hash,processed_hash,
                   not_before,started_at,finished_at,error_message,updated_at
            FROM enrichment_jobs WHERE session_id IN ({placeholders})""",
        ids,
    ).fetchall()
    return {row["session_id"]: dict(row) for row in rows}


def _recent_session_pipeline_candidates(
    conn: sqlite3.Connection, exclude_session_ids: Iterable[str], limit: int = 6
) -> list[tuple[dict, dict]]:
    """Return recent sessions whose derived task structure is still in flight."""
    excluded = list(dict.fromkeys(value for value in exclude_session_ids if value))
    clauses = [
        "j.status IN ('pending','running','failed')",
        "(j.status='running' OR j.priority>=0)",
        "(julianday(j.updated_at) >= julianday('now','-2 days') "
        " OR COALESCE(julianday(s.end_time),julianday(s.start_time),0) "
        ">= julianday('now','-2 days'))",
    ]
    params: list[Any] = []
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        clauses.append(f"s.id NOT IN ({placeholders})")
        params.extend(excluded)
    params.append(max(1, int(limit)))
    rows = conn.execute(
        f"""SELECT s.id,s.title,s.source,s.server_name,s.cwd,
                   s.start_time,s.end_time,s.message_count,
                   si.summary AS insight_summary,
                   si.project_name AS insight_project_name,
                   j.status AS job_status,j.priority AS job_priority,
                   j.reason AS job_reason,j.source_hash,j.processed_hash,
                   j.not_before,j.started_at,j.finished_at,j.error_message,
                   j.updated_at AS job_updated_at
            FROM enrichment_jobs j
            JOIN sessions s ON s.id=j.session_id
            LEFT JOIN session_insights si ON si.session_id=s.id
            WHERE {' AND '.join(clauses)}
            ORDER BY CASE j.status WHEN 'running' THEN 0 ELSE 1 END,
                     COALESCE(julianday(s.end_time),julianday(s.start_time),
                              julianday(j.updated_at),0) DESC,
                     j.priority DESC,j.id DESC
            LIMIT ?""",
        params,
    ).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        job = {
            "status": value.pop("job_status"),
            "priority": value.pop("job_priority"),
            "reason": value.pop("job_reason"),
            "source_hash": value.pop("source_hash"),
            "processed_hash": value.pop("processed_hash"),
            "not_before": value.pop("not_before"),
            "started_at": value.pop("started_at"),
            "finished_at": value.pop("finished_at"),
            "error_message": value.pop("error_message"),
            "updated_at": value.pop("job_updated_at"),
        }
        result.append((value, job))
    return result


def _today_group_activity(group: dict) -> float:
    session = group.get("session") or {}
    pipeline = group.get("pipeline") or {}
    for value in (
        session.get("end_time"), session.get("start_time"),
        pipeline.get("updated_at"),
    ):
        parsed = _parse_time(value)
        if parsed:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    return 0.0


def _build_today_dashboard(compact: bool = False) -> dict:
    # Ask the database for active work directly. Filtering a mixed recent list
    # could hide old-but-open work after a busy day of completed items.
    active, _ = search_work_items(status="active", limit=8)
    stats = get_work_stats()
    if compact:
        inbox, inbox_total = [], stats["inbox"]
    else:
        inbox, inbox_total = search_work_items(review_state="pending", limit=8)
    with get_db() as conn:
        active_sessions = (
            _primary_sessions_for_items(conn, [item["id"] for item in active])
            if compact else {}
        )
        active_groups = []
        if compact:
            compact_active = [
                {key: item.get(key) for key in _TODAY_ACTIVE_FIELDS}
                for item in active
            ]
            active_groups = _group_active_items_by_session(
                compact_active, active_sessions
            )
            session_ids = [
                group["session"]["id"]
                for group in active_groups if group.get("session")
            ]
            jobs_by_session = _enrichment_jobs_for_sessions(conn, session_ids)
            for group in active_groups:
                session = group.get("session")
                group["pipeline"] = (
                    _session_pipeline_state(
                        jobs_by_session.get(session["id"]), bool(group["item_ids"])
                    ) if session else None
                )
            for session, job in _recent_session_pipeline_candidates(
                conn, session_ids
            ):
                active_groups.append({
                    "key": session["id"],
                    "session": session,
                    "item_ids": [],
                    "pipeline": _session_pipeline_state(job, False),
                })
            active_groups.sort(
                key=lambda group: (
                    1 if group.get("session") else 0,
                    _today_group_activity(group),
                ),
                reverse=True,
            )
        completed_rows = conn.execute(
            """SELECT id,title,COUNT(*) OVER() AS total_count
               FROM work_items WHERE status='done'
               AND date(updated_at,'localtime')=date('now','localtime')
               AND merged_into_id IS NULL ORDER BY updated_at DESC LIMIT 8"""
        ).fetchall()
        stale_rows = conn.execute(
            """SELECT id,title,COUNT(*) OVER() AS total_count
               FROM work_items WHERE status IN ('planned','open','in_progress','blocked')
               AND updated_at < datetime('now','-7 days') AND merged_into_id IS NULL
               ORDER BY updated_at LIMIT 8"""
        ).fetchall()
        sessions = [dict(row) for row in conn.execute(
            """SELECT id,title,source,server_name,cwd,start_time,end_time,message_count
               FROM sessions ORDER BY COALESCE(end_time,start_time) DESC LIMIT 10"""
        ).fetchall()]
        similar_solutions = [dict(row) for row in conn.execute(
            """SELECT active.id AS active_id,active.title AS active_title,
                      solved.id AS solved_id,solved.title AS solved_title,
                      r.confidence AS relation_confidence
               FROM work_item_relations r
               JOIN work_items active ON active.id=r.from_item_id
               JOIN work_items solved ON solved.id=r.to_item_id
               WHERE r.relation_type='similar' AND r.confidence>=0.4
                 AND active.status IN ('planned','open','in_progress','blocked')
                 AND solved.status='done'
               UNION ALL
               SELECT active.id,active.title,solved.id,solved.title,
                      r.confidence AS relation_confidence
               FROM work_item_relations r
               JOIN work_items active ON active.id=r.to_item_id
               JOIN work_items solved ON solved.id=r.from_item_id
               WHERE r.relation_type='similar' AND r.confidence>=0.4
                 AND active.status IN ('planned','open','in_progress','blocked')
                 AND solved.status='done'
               ORDER BY relation_confidence DESC LIMIT 8"""
        ).fetchall()]
    completed = [] if compact else [get_work_item(row["id"]) for row in completed_rows]
    stale = [] if compact else [get_work_item(row["id"]) for row in stale_rows]
    recommendations = []
    for item in active:
        if item.get("status") == "blocked":
            recommendations.append({"type": "blocked", "item_id": item["id"], "text": f"处理受阻事项：{item['title']}"})
    for item in stale_rows[:3]:
        recommendations.append({"type": "stale", "item_id": item["id"], "text": f"确认是否继续：{item['title']}"})
    if inbox_total:
        recommendations.insert(0, {"type": "inbox", "text": f"有 {inbox_total} 条整理结果待确认"})
    for match in similar_solutions[:3]:
        recommendations.append({
            "type": "similar_solution",
            "item_id": match["solved_id"],
            "for_item_id": match["active_id"],
            "text": f"“{match['active_title']}”可以参考“{match['solved_title']}”的处理方法",
        })
    result = {
        "date": datetime.now().astimezone().date().isoformat(),
        "stats": stats,
        "active": active,
        "inbox_total": inbox_total,
        "recent_sessions": sessions,
        "recommendations": recommendations[:8],
    }
    if compact:
        result["active"] = compact_active
        result["active_groups"] = active_groups
        result["completed_today_count"] = completed_rows[0]["total_count"] if completed_rows else 0
        result["stale_count"] = stale_rows[0]["total_count"] if stale_rows else 0
    else:
        result["inbox"] = inbox
        result["completed_today"] = [item for item in completed if item]
        result["stale"] = [item for item in stale if item]
    return result


_TODAY_COMPACT_CACHE_TTL_SECONDS = 5.0
_today_compact_condition = threading.Condition()
_today_compact_cache: Optional[dict] = None
_today_compact_cache_key: Optional[tuple[str, int]] = None
_today_compact_cache_expires_at = 0.0
_today_compact_building = False


def _cached_today_dashboard() -> dict:
    """Collapse concurrent Today reads into one database computation.

    Today is requested after sync/Agent state changes and by every visible
    browser.  Without single-flight, a refresh burst repeats the same joins on
    a 14 GiB SQLite file in parallel and exhibits severe CPU/cache contention.
    The database write generation makes mutations invalidate the cache without
    waiting for the short TTL.
    """
    global _today_compact_cache
    global _today_compact_cache_key
    global _today_compact_cache_expires_at
    global _today_compact_building

    key = (get_db_path(), get_db_generation())
    now = time.monotonic()
    with _today_compact_condition:
        if (
            _today_compact_cache is not None
            and _today_compact_cache_key == key
            and now < _today_compact_cache_expires_at
        ):
            return _today_compact_cache

        if _today_compact_building:
            # Serve the previous snapshot while one request refreshes the same
            # database.  On a brand-new database, wait a bounded period for the
            # first builder rather than duplicating expensive work.
            if (
                _today_compact_cache is not None
                and _today_compact_cache_key
                and _today_compact_cache_key[0] == key[0]
            ):
                return _today_compact_cache
            deadline = time.monotonic() + 8.0
            while _today_compact_building:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Today dashboard single-flight timed out")
                _today_compact_condition.wait(timeout=remaining)
            key = (get_db_path(), get_db_generation())
            if _today_compact_cache is not None and _today_compact_cache_key == key:
                return _today_compact_cache

        _today_compact_building = True

    try:
        value = _build_today_dashboard(compact=True)
    except Exception:
        with _today_compact_condition:
            _today_compact_building = False
            _today_compact_condition.notify_all()
        raise

    with _today_compact_condition:
        _today_compact_cache = value
        _today_compact_cache_key = key
        _today_compact_cache_expires_at = time.monotonic() + _TODAY_COMPACT_CACHE_TTL_SECONDS
        _today_compact_building = False
        _today_compact_condition.notify_all()
        return _today_compact_cache


def get_today_dashboard(compact: bool = False) -> dict:
    if compact:
        return _cached_today_dashboard()
    return _build_today_dashboard(compact=False)


def _period_bounds(period_type: str, anchor: Optional[str] = None) -> tuple[datetime, datetime]:
    now = datetime.now().astimezone()
    if anchor:
        parsed = datetime.fromisoformat(anchor)
        now = parsed if parsed.tzinfo else parsed.replace(tzinfo=now.tzinfo)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period_type == "week":
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    elif period_type == "month":
        start = start.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        period_type = "day"
        end = start + timedelta(days=1)
    return start, end


def generate_review(period_type: str = "week", anchor: Optional[str] = None) -> dict:
    start, end = _period_bounds(period_type, anchor)
    items, _ = search_work_items(
        date_from=start.astimezone(timezone.utc).isoformat(),
        date_to=end.astimezone(timezone.utc).isoformat(),
        limit=2000,
    )
    completed = [item for item in items if item.get("status") == "done"]
    blocked = [item for item in items if item.get("status") == "blocked"]
    active = [item for item in items if item.get("status") in ACTIVE_STATUSES]
    project_counts: dict[str, int] = {}
    for item in items:
        name = item.get("project_name") or "未归项目"
        project_counts[name] = project_counts.get(name, 0) + 1
    title = {
        "day": "工作日报", "week": "工作周报", "month": "工作月报"
    }.get(period_type, "工作回顾")
    lines = [
        f"# {title} · {start.date().isoformat()} — {(end - timedelta(days=1)).date().isoformat()}",
        "",
        f"本周期共涉及 **{len(items)}** 项工作，完成 **{len(completed)}** 项，"
        f"进行中 **{len(active)}** 项，阻塞 **{len(blocked)}** 项。",
        "",
        "## 已完成",
    ]
    lines.extend([f"- {item['title']}" for item in completed[:30]] or ["- 暂无已完成事项"])
    lines.extend(["", "## 进行中与下一步"])
    for item in active[:30]:
        next_step = (item.get("next_steps") or [""])[0]
        lines.append(f"- {item['title']}" + (f"：{next_step}" if next_step else ""))
    if not active:
        lines.append("- 暂无进行中事项")
    lines.extend(["", "## 阻塞与风险"])
    lines.extend([f"- {item['title']}" for item in blocked[:20]] or ["- 暂无明确阻塞"])
    lines.extend(["", "## 项目分布"])
    lines.extend(
        f"- {name}：{count} 项" for name, count in sorted(
            project_counts.items(), key=lambda pair: pair[1], reverse=True
        )[:20]
    )
    data = {
        "period_type": period_type,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "counts": {"total": len(items), "done": len(completed), "active": len(active), "blocked": len(blocked)},
        "project_counts": project_counts,
        "item_ids": [item["id"] for item in items],
    }
    review_id = _slug(f"{period_type}-{start.date()}-{end.date()}", "review")
    markdown = "\n".join(lines)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO reviews(
                   id,period_type,period_start,period_end,title,content_markdown,data_json)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(period_type,period_start,period_end) DO UPDATE SET
                   title=excluded.title,content_markdown=excluded.content_markdown,
                   data_json=excluded.data_json,updated_at=datetime('now')""",
            (review_id, period_type, start.isoformat(), end.isoformat(), title, markdown, _dump(data)),
        )
    return {"id": review_id, "title": title, "content_markdown": markdown, "data": data}


def list_reviews(limit: int = 30) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM reviews ORDER BY period_start DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["data"] = _json(value.pop("data_json", None), {})
            value["ai_generated"] = bool(value.get("ai_generated"))
            result.append(value)
        return result


def save_search(name: str, query: str, filters: Optional[dict] = None) -> dict:
    search_id = _slug(f"{name}-{query}", "search")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO saved_searches(id,name,query,filters_json)
               VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,query=excluded.query,
                   filters_json=excluded.filters_json,updated_at=datetime('now')""",
            (search_id, name, query, _dump(filters or {})),
        )
    return {"id": search_id, "name": name, "query": query, "filters": filters or {}}


def list_saved_searches() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM saved_searches ORDER BY updated_at DESC").fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["filters"] = _json(value.pop("filters_json", None), {})
            result.append(value)
        return result


def delete_saved_search(search_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM saved_searches WHERE id=?", (search_id,))
        return cursor.rowcount > 0


def build_context_pack(item_id: str) -> Optional[dict]:
    """Create a portable, evidence-linked context pack for resuming work."""
    item = get_work_item(item_id)
    if not item:
        return None
    lines = [f"# {item['title']}", ""]
    lines.append(
        f"- 项目：{item.get('project_name') or '未归项目'}\n"
        f"- 大任务：{item.get('major_task_title') or '待归类'}\n"
        f"- 状态：{item.get('status')}\n"
        f"- 分类：{item.get('category') or 'other'}"
    )
    for heading, key in (
        ("摘要", "summary"), ("问题 / 目标", "problem"),
        ("已知根因", "root_cause"), ("已采用方案", "solution"),
        ("当前结果", "outcome"),
    ):
        if item.get(key):
            lines.extend(["", f"## {heading}", "", item[key]])
    if item.get("next_steps"):
        lines.extend(["", "## 下一步", ""])
        lines.extend(f"- {step}" for step in item["next_steps"])
    if item.get("entities"):
        lines.extend(["", "## 关键实体", ""])
        lines.extend(
            f"- {entity['entity_type']}: {entity['canonical_name']}"
            for entity in item["entities"]
        )
    commands = list(dict.fromkeys(
        evidence.get("resume_command") for evidence in item.get("evidence", [])
        if evidence.get("resume_command")
    ))
    lines.extend(["", "## 原始证据", ""])
    for evidence in item.get("evidence", [])[:20]:
        lines.append(
            f"- {evidence.get('source')} · {evidence.get('session_title') or evidence.get('session_id')}"
            f" · message_id={evidence.get('message_id')}"
        )
    if commands:
        lines.extend(["", "## 恢复命令", ""])
        lines.extend(f"```bash\n{command}\n```" for command in commands)
    lines.extend([
        "",
        "## 给 Agent 的继续工作约束",
        "",
        "请先核验仓库和运行环境的当前状态，再从“下一步”继续；历史结论仅作为证据，若代码或环境已变化应重新验证。",
    ])
    return {
        "item_id": item_id,
        "title": item["title"],
        "markdown": "\n".join(lines),
        "resume_commands": commands,
        "session_ids": list(dict.fromkeys(
            evidence["session_id"] for evidence in item.get("evidence", [])
        )),
        "generated_at": _now(),
    }


def get_timeline(days: int = 30, limit: int = 500) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    with get_db() as conn:
        session_rows = conn.execute(
            """SELECT id,title,source,server_name,cwd,start_time,end_time,message_count
               FROM sessions WHERE COALESCE(end_time,start_time)>=?
               ORDER BY COALESCE(end_time,start_time) DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        item_rows = conn.execute(
            """SELECT id,title,kind,status,project_name,category,
                      source_start_time,source_end_time,updated_at
               FROM work_items WHERE merged_into_id IS NULL
                 AND COALESCE(source_end_time,source_start_time,updated_at)>=?
               ORDER BY COALESCE(source_end_time,source_start_time,updated_at) DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
    events = [
        {
            "type": "session",
            "id": row["id"],
            "title": row["title"] or row["id"],
            "time": row["end_time"] or row["start_time"],
            "source": row["source"],
            "server_name": row["server_name"],
            "cwd": row["cwd"],
            "message_count": row["message_count"],
        }
        for row in session_rows
    ]
    events.extend(
        {
            "type": "work_item",
            "id": row["id"],
            "title": row["title"],
            "time": row["source_end_time"] or row["source_start_time"] or row["updated_at"],
            "kind": row["kind"],
            "status": row["status"],
            "project_name": row["project_name"],
            "category": row["category"],
        }
        for row in item_rows
    )
    events.sort(key=lambda event: event.get("time") or "", reverse=True)
    grouped: dict[str, list[dict]] = {}
    for event in events[:limit]:
        date = (event.get("time") or "")[:10] or "未知日期"
        grouped.setdefault(date, []).append(event)
    return {"days": days, "events": events[:limit], "groups": grouped}


def get_knowledge_dashboard(
    limit: int = 100,
    *,
    query: str = "",
    project_id: Optional[str] = None,
    kind: Optional[str] = None,
    offset: int = 0,
) -> dict:
    selected_kinds = (
        (kind,) if kind in ("solution", "knowledge")
        else ("solution", "knowledge")
    )
    matched_parent_ids: set[str] = set()
    solutions, solution_total = (
        search_work_items(
            query, kind="solution", project_id=project_id, limit=limit,
            parent_ids_out=matched_parent_ids,
        ) if "solution" in selected_kinds else ([], 0)
    )
    knowledge, knowledge_total = (
        search_work_items(
            query, kind="knowledge", project_id=project_id, limit=limit,
            parent_ids_out=matched_parent_ids,
        ) if "knowledge" in selected_kinds else ([], 0)
    )
    topics, topic_total = list_major_tasks(
        query,
        project_id=project_id,
        child_kinds=selected_kinds,
        limit=limit,
        offset=offset,
        knowledge_only=True,
        matched_parent_ids=matched_parent_ids,
    )
    with get_db() as conn:
        recurring = [dict(row) for row in conn.execute(
            """SELECT w.id,w.title,w.kind,w.project_name,w.category,
                      COUNT(r.id) AS related_count,
                      MAX(r.confidence) AS max_confidence
               FROM work_items w JOIN work_item_relations r
                 ON r.from_item_id=w.id OR r.to_item_id=w.id
               WHERE r.relation_type='similar' AND w.merged_into_id IS NULL
               GROUP BY w.id HAVING COUNT(r.id)>0
               ORDER BY related_count DESC,max_confidence DESC LIMIT ?""",
            (limit,),
        ).fetchall()]
        entities = [dict(row) for row in conn.execute(
            """SELECT e.id,e.entity_type,e.canonical_name,COUNT(we.work_item_id) item_count
               FROM entities e JOIN work_item_entities we ON we.entity_id=e.id
               GROUP BY e.id ORDER BY item_count DESC,e.canonical_name LIMIT 50"""
        ).fetchall()]
    return {
        "query": query,
        "topics": topics,
        "topic_total": topic_total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(topics) < topic_total,
        "solutions": solutions,
        "solution_total": solution_total,
        "knowledge": knowledge,
        "knowledge_total": knowledge_total,
        "recurring_patterns": recurring,
        "top_entities": entities,
    }


def find_similar_items(item_id: str, limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT r.relation_type,r.confidence,w.id,w.title,w.kind,w.status,
                      w.project_name,w.summary,w.problem,w.solution,w.outcome
               FROM work_item_relations r JOIN work_items w ON w.id=
                 CASE WHEN r.from_item_id=? THEN r.to_item_id ELSE r.from_item_id END
               WHERE (r.from_item_id=? OR r.to_item_id=?)
                 AND w.merged_into_id IS NULL
               ORDER BY r.confidence DESC,w.updated_at DESC LIMIT ?""",
            (item_id, item_id, item_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
