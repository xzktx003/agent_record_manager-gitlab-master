"""MCP-compatible API for agent access."""
from fastapi import APIRouter, Query
from typing import Optional
from app.database import (
    DEFAULT_MESSAGE_PAGE_SIZE,
    DEFAULT_MESSAGE_PREVIEW_CHARS,
    get_session_detail,
    get_sessions,
    get_stats,
    search_messages,
)
from app.smart_search import quick_search_payload
from app.work_memory import (
    build_context_pack,
    find_similar_items,
    get_today_dashboard,
    get_work_item,
    update_work_item,
)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

@router.get("/search")
def mcp_search(q: str, limit: int = 20):
    """Search conversations - designed for agent/MCP consumption."""
    results = search_messages(query=q, limit=limit)
    # Return compact format for agents
    compact = []
    for r in results:
        compact.append({
            "session_id": r["session_id"],
            "session_title": r.get("title", ""),
            "role": r["role"],
            "snippet": r.get("snippet", r.get("content", "")[:200]),
            "timestamp": r.get("timestamp"),
            "source": r.get("source"),
            "server": r.get("server_name"),
        })
    return {"results": compact, "query": q}

@router.get("/session/{session_id}")
def mcp_session(
    session_id: str,
    roles: str = "user,assistant",
    msg_offset: int = Query(0, ge=0),
    msg_limit: int = Query(DEFAULT_MESSAGE_PAGE_SIZE, ge=1, le=500),
    content_limit: int = Query(
        DEFAULT_MESSAGE_PREVIEW_CHARS, ge=0, le=500000
    ),
):
    """Get one bounded session page, filtered for readability."""
    detail = get_session_detail(
        session_id,
        msg_offset=msg_offset,
        msg_limit=msg_limit,
        content_limit=content_limit,
    )
    if not detail:
        return {"error": "not found"}
    allowed_roles = set(roles.split(","))
    filtered_msgs = [
        {
            "id": m["id"],
            "role": m["role"],
            "content": m["content"],
            "content_length": m.get("content_length"),
            "content_truncated": bool(m.get("content_truncated")),
            "timestamp": m.get("timestamp"),
        }
        for m in detail.get("messages", [])
        if m.get("role") in allowed_roles and m.get("content")
    ]
    return {
        "id": detail["id"],
        "title": detail.get("title"),
        "source": detail.get("source"),
        "start_time": detail.get("start_time"),
        "messages": filtered_msgs,
        "message_page": {
            "offset": detail["msg_offset"],
            "limit": detail["msg_limit"],
            "start": detail["msg_start"],
            "end": detail["msg_end"],
            "total": detail["msg_total"],
            "has_previous": detail["msg_has_previous"],
            "has_more": detail["msg_has_more"],
        },
    }

@router.get("/summary")
def mcp_summary(days: int = 7):
    """Get recent activity summary for agents."""
    stats = get_stats()
    return {
        "total_sessions": stats["total_sessions"],
        "total_messages": stats["total_messages"],
        "by_source": stats["by_source"],
        "by_server": stats["by_server"],
        "recent_titles": [
            {"id": s["id"], "title": s.get("title", ""), "time": s.get("start_time")}
            for s in stats["recent_sessions"]
        ],
    }

@router.get("/sessions")
def mcp_sessions(search: Optional[str] = None, limit: int = 20):
    """List sessions with optional search."""
    sessions, total = get_sessions(limit=limit, search=search)
    return {
        "sessions": [
            {
                "id": s["id"],
                "title": s.get("title", ""),
                "source": s.get("source"),
                "server": s.get("server_name"),
                "start_time": s.get("start_time"),
                "message_count": s.get("message_count", 0),
            }
            for s in sessions
        ],
        "total": total,
    }


@router.get("/work/search")
def mcp_work_search(
    q: str,
    source: Optional[str] = None,
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
):
    """Search evidence-backed work items; raw sessions remain as fallback."""
    return quick_search_payload(
        q,
        {"source": source, "status": status, "project_id": project_id},
        limit=limit,
    )


@router.get("/work/item/{item_id}")
def mcp_work_item(item_id: str):
    item = get_work_item(item_id)
    return item or {"error": "not found"}


@router.get("/work/item/{item_id}/context-pack")
def mcp_work_context_pack(item_id: str):
    return build_context_pack(item_id) or {"error": "not found"}


@router.get("/work/item/{item_id}/similar")
def mcp_similar_work(item_id: str, limit: int = 10):
    return {"items": find_similar_items(item_id, limit)}


@router.get("/work/today")
def mcp_work_today():
    return get_today_dashboard()


@router.post("/work/item/{item_id}/status")
def mcp_update_work_status(item_id: str, status: str):
    item = update_work_item(item_id, {"status": status}, action="mcp_status")
    return item or {"error": "not found"}
