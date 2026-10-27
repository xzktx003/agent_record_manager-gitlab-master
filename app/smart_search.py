"""Progressive Agent-assisted search and evidence-grounded answers."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.agent_provider import (
    QUERY_PLAN_PROMPT_VERSION,
    REVIEW_PROMPT_VERSION,
    SEARCH_ANSWER_PROMPT_VERSION,
    AgentProviderError,
    answer_search_results,
    get_agent_provider,
    improve_review,
    plan_search_query,
)
from app.config import get_config
from app.database import get_db, search_sessions
from app.enrichment import enrichment_manager
from app.tenant import get_data_scope, tenant_scope
from app.work_memory import get_work_item, list_major_tasks, search_work_items


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


_RAW_RESULT_FIELDS = (
    "id", "session_id", "role", "content_length", "content_truncated",
    "timestamp", "msg_type", "source", "server_name", "title", "cwd",
    "start_time", "end_time", "match_count", "match_type",
    "indexed_message_coverage", "message_coverage", "role_priority",
    "matched_terms", "term_count", "term_coverage", "relevance_score",
    "display_title", "session_tag", "agent_match_reason",
)


def _compact_raw_results(rows: list[dict]) -> list[dict]:
    """Keep search-job responses small while preserving navigation metadata.

    Raw FTS rows can carry up to 16 KiB of message text each. The UI already
    renders the highlighted snippet and opens the canonical session on click,
    so returning dozens of full message prefixes only slows polling and stores
    unnecessarily large JSON blobs in SQLite.
    """
    compact = []
    for row in rows:
        item = {key: row.get(key) for key in _RAW_RESULT_FIELDS if key in row}
        item["snippet"] = row.get("snippet") or ""
        # Retain a bounded plain-text fallback for metadata-only matches.
        item["content"] = (row.get("content") or "")[:800]
        compact.append(item)
    return compact


def _record_run(
    run_type: str,
    provider: str,
    model: str,
    prompt_version: str,
    request: dict,
    result=None,
    error: Optional[str] = None,
) -> None:
    run_id = f"run-{uuid.uuid4().hex}"
    started = _now()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO agent_runs(
                   id,run_type,provider,model,status,input_hash,prompt_version,
                   request_json,response_json,error_message,duration_ms,
                   started_at,finished_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                run_type,
                provider,
                model,
                "error" if error else "success",
                hashlib.sha256(_dump(request).encode()).hexdigest(),
                prompt_version,
                _dump(request),
                _dump(result.data) if result else None,
                error,
                result.duration_ms if result else 0,
                started,
                _now(),
            ),
        )


def quick_search_payload(
    query: str,
    filters: dict,
    limit: int = 50,
    offset: int = 0,
    include_raw: bool = True,
    raw_offset: Optional[int] = None,
) -> dict:
    matched_parent_ids: set[str] = set()
    items, total = search_work_items(
        query,
        kind=filters.get("kind") or None,
        status=filters.get("status") or None,
        project_id=filters.get("project_id") or None,
        review_state=filters.get("review_state") or None,
        source=filters.get("source") or None,
        date_from=filters.get("date_from") or None,
        date_to=filters.get("date_to") or None,
        parent_ids_out=matched_parent_ids,
        limit=limit,
        offset=offset,
    )
    major_tasks, major_total = list_major_tasks(
        query,
        status=filters.get("status") or None,
        project_id=filters.get("project_id") or None,
        kind=filters.get("kind") or None,
        limit=min(12, limit),
        offset=0,
        matched_parent_ids=matched_parent_ids,
    )
    raw_results: list[dict] = []
    raw_total = 0
    raw_next_offset = raw_offset if raw_offset is not None else offset
    raw_has_more = False
    # Until all history is enriched, raw FTS remains a first-class fallback.
    # It also prevents Agent/model outages from hiding existing records.
    if query and include_raw:
        resolved_raw_offset = offset if raw_offset is None else max(0, raw_offset)
        raw_results, raw_total = search_sessions(
            query,
            limit=min(20, limit),
            offset=resolved_raw_offset,
            source=filters.get("source") or None,
            date_from=filters.get("date_from") or None,
            date_to=filters.get("date_to") or None,
        )
        raw_next_offset = resolved_raw_offset + len(raw_results)
        raw_has_more = raw_next_offset < raw_total
        item_session_ids = set()
        for item in items:
            detail = get_work_item(item["id"])
            if detail:
                item_session_ids.update(
                    evidence.get("session_id") for evidence in detail.get("evidence", [])
                )
        raw_results = [
            row for row in raw_results if row.get("session_id") not in item_session_ids
        ]
    return {
        "query": query,
        "results": items,
        "total": total,
        "major_tasks": major_tasks,
        "major_total": major_total,
        "raw_sessions": _compact_raw_results(raw_results),
        "raw_total": raw_total,
        "result_unit": "work_item",
        "mode": "quick",
        "has_more": offset + len(items) < total,
        "raw_offset": raw_offset if raw_offset is not None else offset,
        "raw_next_offset": raw_next_offset,
        "raw_has_more": raw_has_more,
    }


class SmartSearchManager:
    """Runs interactive Agent searches outside the HTTP request thread."""

    def __init__(self, max_concurrent: int = 2) -> None:
        self._semaphore = threading.BoundedSemaphore(max_concurrent)
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def submit(self, query: str, filters: Optional[dict] = None) -> dict:
        query = (query or "").strip()
        if not query:
            raise ValueError("query is required")
        filters = filters or {}
        job_id = f"search-{uuid.uuid4().hex}"
        with get_db() as conn:
            conn.execute(
                """INSERT INTO smart_search_jobs(id,query,filters_json,status)
                   VALUES (?,?,?,'queued')""",
                (job_id, query, _dump(filters)),
            )
        scope = get_data_scope()
        thread = threading.Thread(
            target=self._run_for_scope, args=(scope, job_id, query, filters),
            name=f"smart-search-{job_id[-8:]}", daemon=True,
        )
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        return {"job_id": job_id, "status": "queued"}

    def _run_for_scope(
        self, scope: str, job_id: str, query: str, filters: dict
    ) -> None:
        # The result and fallback both query SQLite, so the entire background
        # lifecycle must stay in the submitting user's physical database.
        with tenant_scope(scope):
            self._run(job_id, query, filters)

    def get(self, job_id: str) -> Optional[dict]:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM smart_search_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["filters"] = _load(value.pop("filters_json", None), {})
        value["plan"] = _load(value.pop("plan_json", None), None)
        value["result"] = _load(value.pop("result_json", None), None)
        return value

    def _run(self, job_id: str, query: str, filters: dict) -> None:
        with self._semaphore:
            with get_db() as conn:
                conn.execute(
                    "UPDATE smart_search_jobs SET status='running',started_at=? WHERE id=?",
                    (_now(), job_id),
                )
            try:
                payload = self._execute(query, filters)
                with get_db() as conn:
                    conn.execute(
                        """UPDATE smart_search_jobs SET status='completed',plan_json=?,
                                  result_json=?,finished_at=? WHERE id=?""",
                        (_dump(payload.get("plan")), _dump(payload), _now(), job_id),
                    )
            except Exception as exc:
                # Even a total Agent failure returns deterministic quick search.
                fallback = quick_search_payload(query, filters)
                fallback.update({
                    "mode": "degraded",
                    "warning": f"Agent 辅助查找暂不可用，已保留关键词结果：{exc}",
                    "plan": None,
                    "answer": None,
                })
                with get_db() as conn:
                    conn.execute(
                        """UPDATE smart_search_jobs SET status='completed',
                                  result_json=?,error_message=?,finished_at=? WHERE id=?""",
                        (_dump(fallback), str(exc)[-4000:], _now(), job_id),
                    )
            finally:
                with self._lock:
                    self._threads.pop(job_id, None)

    def _execute(self, query: str, filters: dict) -> dict:
        config = get_config().agent
        if not config.enabled:
            raise AgentProviderError("Agent 辅助功能已关闭")
        provider = get_agent_provider()

        plan_result = plan_search_query(provider, query, filters)
        _record_run(
            "query_plan", provider.name, plan_result.model,
            QUERY_PLAN_PROMPT_VERSION,
            {"query": query, "filters": filters}, plan_result,
        )
        plan = plan_result.data
        effective_filters = dict(filters)
        valid_sources = {
            "codex", "claude", "copilot", "opencode", "vscode-chat", "vscode-memory",
            "vscode-chat-edit", "vscode-panel", "vscode-debug-log",
        }
        planned_source = str(plan.get("source") or "").strip().casefold()
        if planned_source not in valid_sources:
            planned_source = ""
        planned_status = str(plan.get("status") or "").strip().casefold()
        if planned_status not in {
            "planned", "open", "in_progress", "blocked", "done",
            "cancelled", "archived", "active",
        }:
            planned_status = ""
        plan["source"] = planned_source
        plan["status"] = planned_status
        for date_key in ("date_from", "date_to"):
            date_value = str(plan.get(date_key) or "").strip()
            plan[date_key] = date_value if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", date_value) else ""
        for key in ("source", "status", "date_from", "date_to"):
            if not effective_filters.get(key) and plan.get(key):
                effective_filters[key] = plan[key]
        if not effective_filters.get("project_id") and plan.get("project"):
            with get_db() as conn:
                project = conn.execute(
                    "SELECT id FROM projects WHERE name LIKE ? ORDER BY updated_at DESC LIMIT 1",
                    (f"%{plan['project']}%",),
                ).fetchone()
            if project:
                effective_filters["project_id"] = project["id"]
        extra_terms = [
            *plan.get("required_terms", []),
            *plan.get("optional_terms", []),
            *plan.get("aliases", []),
        ]
        search_query = plan.get("rewritten_query") or query
        items, total = search_work_items(
            search_query,
            kind=effective_filters.get("kind") or None,
            status=effective_filters.get("status") or None,
            project_id=effective_filters.get("project_id") or None,
            review_state=effective_filters.get("review_state") or None,
            source=effective_filters.get("source") or None,
            date_from=effective_filters.get("date_from") or None,
            date_to=effective_filters.get("date_to") or None,
            extra_terms=extra_terms,
            limit=40,
        )
        raw_results, raw_total = search_sessions(
            search_query,
            limit=20,
            source=effective_filters.get("source") or None,
            date_from=effective_filters.get("date_from") or None,
            date_to=effective_filters.get("date_to") or None,
        )

        raw_candidates = [
            {
                "id": f"session:{row.get('session_id')}",
                "title": row.get("display_title") or row.get("title") or row.get("session_id"),
                "project_name": row.get("cwd") or "",
                "status": "raw_session",
                "summary": row.get("content") or row.get("snippet") or "",
                "problem": row.get("content") or "",
                "root_cause": "",
                "solution": "",
                "outcome": "",
                "keywords": row.get("matched_terms") or [],
                "sources": [row.get("source")] if row.get("source") else [],
                "source_end_time": row.get("end_time") or row.get("timestamp"),
                "matched_terms": row.get("matched_terms") or [],
            }
            for row in raw_results[:15]
        ]
        answer_candidates = [*items[:20], *raw_candidates]
        answer = None
        if answer_candidates:
            answer_result = answer_search_results(
                provider, query, plan, answer_candidates
            )
            _record_run(
                "search_answer", provider.name, answer_result.model,
                SEARCH_ANSWER_PROMPT_VERSION,
                {"query": query, "plan": plan, "candidate_ids": [item["id"] for item in answer_candidates]},
                answer_result,
            )
            answer = answer_result.data
            order = {item_id: index for index, item_id in enumerate(answer.get("best_item_ids", []))}
            explanations = {
                row.get("item_id"): row.get("reason")
                for row in answer.get("why_matched", [])
            }
            for item in items:
                if item["id"] in explanations:
                    item["agent_match_reason"] = explanations[item["id"]]
            for row in raw_results:
                raw_id = f"session:{row.get('session_id')}"
                if raw_id in explanations:
                    row["agent_match_reason"] = explanations[raw_id]
            items.sort(
                key=lambda item: (
                    0 if item["id"] in order else 1,
                    order.get(item["id"], 9999),
                    -float(item.get("relevance_score") or 0),
                )
            )
            raw_results.sort(
                key=lambda row: (
                    0 if f"session:{row.get('session_id')}" in order else 1,
                    order.get(f"session:{row.get('session_id')}", 9999),
                    -float(row.get("relevance_score") or 0),
                )
            )

        if raw_results and len(items) < 5:
            for raw in raw_results[:3]:
                try:
                    enrichment_manager.enqueue_session(
                        raw["session_id"], priority=80, force=False
                    )
                except (ValueError, KeyError):
                    pass
        return {
            "query": query,
            "effective_query": search_query,
            "effective_filters": effective_filters,
            "plan": plan,
            "answer": answer,
            "results": items,
            "total": total,
            "raw_sessions": _compact_raw_results(raw_results),
            "raw_total": raw_total,
            "result_unit": "work_item",
            "mode": "agent",
        }


def enhance_review(review: dict) -> dict:
    provider = get_agent_provider()
    result = improve_review(provider, review)
    _record_run(
        "review", provider.name, result.model, REVIEW_PROMPT_VERSION,
        {"review_id": review.get("id"), "data": review.get("data")}, result,
    )
    data = result.data
    lines = [f"# {data['title']}", "", data["executive_summary"], ""]
    for heading, key in (
        ("亮点与完成", "wins"), ("阻塞与风险", "blockers"),
        ("重复模式", "patterns"), ("下一阶段重点", "next_focus"),
    ):
        lines.extend([f"## {heading}", ""])
        lines.extend([f"- {value}" for value in data.get(key, [])] or ["- 暂无"])
        lines.append("")
    review["content_markdown"] = "\n".join(lines).strip()
    review["agent_review"] = data
    with get_db() as conn:
        conn.execute(
            """UPDATE reviews SET title=?,content_markdown=?,data_json=?,
                      ai_generated=1,updated_at=datetime('now') WHERE id=?""",
            (
                data["title"], review["content_markdown"],
                _dump({**review.get("data", {}), "agent_review": data}), review["id"],
            ),
        )
    return review


smart_search_manager = SmartSearchManager()
