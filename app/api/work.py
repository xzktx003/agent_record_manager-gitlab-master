"""Personal work-memory, Agent enrichment and smart-search API."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.agent_provider import AgentProviderError
from app.database import get_db
from app.enrichment import enrichment_manager
from app.smart_search import (
    enhance_review,
    quick_search_payload,
    smart_search_manager,
)
from app.work_memory import (
    approve_work_items,
    build_context_pack,
    create_work_item,
    delete_saved_search,
    generate_review,
    get_project,
    get_today_dashboard,
    get_timeline,
    get_knowledge_dashboard,
    get_major_task,
    get_work_item,
    get_work_stats,
    list_projects,
    list_major_tasks,
    list_reviews,
    list_saved_searches,
    find_similar_items,
    merge_work_items,
    save_search,
    search_work_items,
    split_work_item,
    update_work_item,
)


router = APIRouter(prefix="/api/work", tags=["work-memory"])


class WorkItemPayload(BaseModel):
    kind: str = "task"
    title: str
    project_name: str = ""
    category: str = "other"
    status: str = "open"
    priority: str = "normal"
    summary: str = ""
    problem: str = ""
    root_cause: str = ""
    solution: str = ""
    outcome: str = ""
    next_steps: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    major_task_id: str = ""
    major_task_title: str = ""


class WorkItemPatch(BaseModel):
    kind: Optional[str] = None
    title: Optional[str] = None
    project_name: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    summary: Optional[str] = None
    problem: Optional[str] = None
    root_cause: Optional[str] = None
    solution: Optional[str] = None
    outcome: Optional[str] = None
    next_steps: Optional[list[str]] = None
    keywords: Optional[list[str]] = None
    review_state: Optional[str] = None
    major_task_id: Optional[str] = None
    major_task_title: Optional[str] = None


class MergePayload(BaseModel):
    source_ids: list[str]


class SplitPayload(BaseModel):
    title: str
    evidence_ids: list[int] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)


class SmartSearchPayload(BaseModel):
    query: str
    filters: dict[str, Any] = Field(default_factory=dict)


class SavedSearchPayload(BaseModel):
    name: str
    query: str
    filters: dict[str, Any] = Field(default_factory=dict)


class BulkApprovePayload(BaseModel):
    item_ids: list[str] = Field(default_factory=list, max_length=500)
    all_pending: bool = False
    expected_total: Optional[int] = Field(default=None, ge=0)


class QuickSearchWorkItem(BaseModel):
    """Stable public fields; additive source fields remain available to clients."""

    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    kind: str = "task"
    status: str = "open"
    review_state: str = "pending"
    project_name: Optional[str] = None
    summary: Optional[str] = None
    outcome: Optional[str] = None
    updated_at: str
    evidence_count: int = 0
    sources: list[str] = Field(default_factory=list)


class QuickSearchSessionHit(BaseModel):
    """One representative message for a matching raw Agent session."""

    model_config = ConfigDict(extra="allow")

    id: int | str | None = None
    session_id: str
    display_title: Optional[str] = None
    title: Optional[str] = None
    source: Optional[str] = None
    timestamp: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    snippet: str = ""
    content: str = ""


class QuickSearchResponse(BaseModel):
    query: str
    results: list[QuickSearchWorkItem] = Field(default_factory=list)
    total: int = Field(ge=0)
    major_tasks: list[dict[str, Any]] = Field(default_factory=list)
    major_total: int = Field(default=0, ge=0)
    raw_sessions: list[QuickSearchSessionHit] = Field(default_factory=list)
    raw_total: int = Field(ge=0)
    result_unit: str = "work_item"
    mode: str = "quick"
    has_more: bool = False
    raw_offset: int = Field(ge=0)
    raw_next_offset: int = Field(ge=0)
    raw_has_more: bool = False


def _model_data(model: BaseModel, *, exclude_none: bool = False) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=exclude_none)
    return model.dict(exclude_none=exclude_none)


@router.get("/today")
def today(response: Response, compact: bool = False):
    response.headers["Cache-Control"] = "no-store"
    return get_today_dashboard(compact=compact)


@router.get("/stats")
def stats():
    return get_work_stats()


@router.get("/items")
def items(
    q: str = "",
    kind: Optional[str] = None,
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    review_state: Optional[str] = None,
    source: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    results, total = search_work_items(
        q,
        kind=kind,
        status=status,
        project_id=project_id,
        review_state=review_state,
        source=source,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return {
        "items": results,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(results) < total,
    }


@router.get("/major-tasks")
def major_tasks(
    q: str = "",
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    results, total = list_major_tasks(
        q,
        status=status,
        project_id=project_id,
        kind=kind,
        limit=limit,
        offset=offset,
    )
    return {
        "major_tasks": results,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(results) < total,
    }


@router.get("/major-tasks/{major_task_id}")
def major_task_detail(
    major_task_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    result = get_major_task(major_task_id, limit=limit, offset=offset)
    if not result:
        raise HTTPException(status_code=404, detail="Major task not found")
    return result


@router.post("/items")
def create_item(payload: WorkItemPayload):
    data = _model_data(payload)
    data["project"] = data.pop("project_name", "")
    return create_work_item(data)


@router.get("/items/{item_id}")
def item_detail(item_id: str):
    item = get_work_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Work item not found")
    return item


@router.get("/items/{item_id}/context-pack")
def context_pack(item_id: str):
    pack = build_context_pack(item_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Work item not found")
    return pack


@router.get("/items/{item_id}/similar")
def similar_items(item_id: str, limit: int = Query(20, ge=1, le=100)):
    if not get_work_item(item_id):
        raise HTTPException(status_code=404, detail="Work item not found")
    return {"items": find_similar_items(item_id, limit)}


@router.patch("/items/{item_id}")
def edit_item(item_id: str, payload: WorkItemPatch):
    item = update_work_item(item_id, _model_data(payload, exclude_none=True))
    if not item:
        raise HTTPException(status_code=404, detail="Work item not found")
    return item


@router.post("/items/{item_id}/approve")
def approve_item(item_id: str):
    item = update_work_item(item_id, {"review_state": "approved"}, "approve")
    if not item:
        raise HTTPException(status_code=404, detail="Work item not found")
    return item


@router.post("/items/bulk-approve")
def bulk_approve_items(payload: BulkApprovePayload):
    if payload.all_pending and payload.item_ids:
        raise HTTPException(
            status_code=400,
            detail="Choose either item_ids or all_pending, not both",
        )
    if not payload.all_pending and not payload.item_ids:
        raise HTTPException(status_code=400, detail="No work items selected")
    if payload.all_pending and payload.expected_total is None:
        raise HTTPException(
            status_code=400,
            detail="expected_total is required when approving all pending items",
        )
    result = approve_work_items(
        payload.item_ids,
        all_pending=payload.all_pending,
        expected_total=payload.expected_total,
    )
    if result.get("conflict"):
        raise HTTPException(
            status_code=409,
            detail="待确认列表已发生变化，请刷新后重新全选",
        )
    return result


@router.post("/items/{item_id}/dismiss")
def dismiss_item(item_id: str):
    item = update_work_item(
        item_id, {"review_state": "dismissed", "status": "archived"}, "dismiss"
    )
    if not item:
        raise HTTPException(status_code=404, detail="Work item not found")
    return item


@router.post("/items/{item_id}/merge")
def merge_item(item_id: str, payload: MergePayload):
    item = merge_work_items(item_id, payload.source_ids)
    if not item:
        raise HTTPException(status_code=404, detail="Target work item not found")
    return item


@router.post("/items/{item_id}/split")
def split_item(item_id: str, payload: SplitPayload):
    fields = dict(payload.fields)
    fields["title"] = payload.title
    item = split_work_item(item_id, fields, payload.evidence_ids)
    if not item:
        raise HTTPException(status_code=404, detail="Work item not found")
    return item


@router.get("/inbox")
def inbox(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    results, total = search_work_items(
        review_state="pending", limit=limit, offset=offset
    )
    return {"items": results, "total": total, "limit": limit, "offset": offset}


@router.get("/projects")
def projects(limit: int = Query(200, ge=1, le=1000)):
    return {"projects": list_projects(limit)}


@router.get("/projects/{project_id}")
def project_detail(project_id: str):
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/timeline")
def timeline(
    days: int = Query(30, ge=1, le=3650),
    limit: int = Query(500, ge=1, le=2000),
):
    return get_timeline(days, limit)


@router.get("/knowledge")
def knowledge(
    q: str = "",
    project_id: Optional[str] = None,
    kind: Optional[str] = Query(None, pattern="^(solution|knowledge)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return get_knowledge_dashboard(
        limit,
        query=q,
        project_id=project_id,
        kind=kind,
        offset=offset,
    )


@router.get("/search", response_model=QuickSearchResponse)
def quick_search(
    response: Response,
    q: str = Query(..., min_length=1),
    source: Optional[str] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    review_state: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    raw_offset: int = Query(0, ge=0),
    include_raw: bool = False,
):
    response.headers["Cache-Control"] = "no-store"
    filters = {
        "source": source,
        "kind": kind,
        "status": status,
        "project_id": project_id,
        "review_state": review_state,
        "date_from": date_from,
        "date_to": date_to,
    }
    return quick_search_payload(
        q.strip(),
        filters,
        limit,
        offset,
        include_raw,
        raw_offset=raw_offset,
    )


@router.post("/search/agent", status_code=202)
def start_smart_search(payload: SmartSearchPayload):
    try:
        return smart_search_manager.submit(payload.query, payload.filters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/search/agent/{job_id}")
def smart_search_status(job_id: str):
    job = smart_search_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Search job not found")
    return job


@router.get("/reviews")
def reviews(limit: int = Query(30, ge=1, le=200)):
    return {"reviews": list_reviews(limit)}


@router.post("/reviews/generate")
def create_review(
    period: str = Query("week", pattern="^(day|week|month)$"),
    anchor: Optional[str] = None,
    smart: bool = False,
):
    review = generate_review(period, anchor)
    if smart:
        try:
            review = enhance_review(review)
        except AgentProviderError as exc:
            review["agent_warning"] = str(exc)
    return review


@router.get("/saved-searches")
def saved_searches():
    return {"saved_searches": list_saved_searches()}


@router.post("/saved-searches")
def create_saved_search(payload: SavedSearchPayload):
    return save_search(payload.name, payload.query, payload.filters)


@router.delete("/saved-searches/{search_id}")
def remove_saved_search(search_id: str):
    if not delete_saved_search(search_id):
        raise HTTPException(status_code=404, detail="Saved search not found")
    return {"deleted": True, "id": search_id}


@router.get("/agent/status")
def agent_status(compact: bool = False):
    return enrichment_manager.status(compact=compact)


@router.post("/agent/pause")
def pause_agent():
    return enrichment_manager.pause()


@router.post("/agent/resume")
def resume_agent():
    return enrichment_manager.resume()


@router.post("/agent/retry-failed")
def retry_failed():
    return {"retried": enrichment_manager.retry_failed()}


@router.post("/agent/enqueue/{session_id}")
def enqueue_session(session_id: str, force: bool = False):
    try:
        return enrichment_manager.enqueue_session(session_id, force=force)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agent/backfill")
def backfill(
    days: Optional[int] = Query(None, ge=1, le=36500),
    source: Optional[str] = None,
    force: bool = False,
):
    return enrichment_manager.enqueue_history(days, source, force)


@router.get("/sessions/{session_id}/insight")
def session_insight(session_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM session_insights WHERE session_id=?", (session_id,)
        ).fetchone()
        items = conn.execute(
            """SELECT DISTINCT w.id,w.title,w.kind,w.status,w.review_state
               FROM work_items w JOIN work_item_sources ws ON ws.work_item_id=w.id
               WHERE ws.session_id=? AND w.merged_into_id IS NULL
               ORDER BY w.updated_at DESC""",
            (session_id,),
        ).fetchall()
    if not row:
        return {"session_id": session_id, "status": "not_analyzed", "items": []}
    data = dict(row)
    for key, default in (("tags_json", []), ("analysis_json", {})):
        try:
            data[key.removesuffix("_json")] = __import__("json").loads(data.pop(key) or "null") or default
        except (ValueError, TypeError):
            data[key.removesuffix("_json")] = default
    data["items"] = [dict(item) for item in items]
    data["status"] = "analyzed"
    return data
