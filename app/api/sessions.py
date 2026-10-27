"""Sessions API endpoints."""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.database import (
    DEFAULT_MESSAGE_PAGE_SIZE,
    DEFAULT_MESSAGE_PREVIEW_CHARS,
    MESSAGE_VIEWS,
    get_message_content,
    get_sessions,
    get_session_detail,
    get_stats,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionMessageResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    session_id: str
    role: str
    content: Optional[str] = None
    msg_type: Optional[str] = None
    timestamp: Optional[str] = None
    content_length: int = Field(default=0, ge=0)
    content_truncated: bool = False


class SessionDetailResponse(BaseModel):
    """Bounded session window used by Work Hub and other Agent clients."""

    model_config = ConfigDict(extra="allow")

    id: str
    source: str
    title: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    updated_at: Optional[str] = None
    message_count: int = Field(default=0, ge=0)
    messages: list[SessionMessageResponse] = Field(default_factory=list)
    msg_total: int = Field(ge=0)
    msg_total_unfiltered: int = Field(ge=0)
    message_view: str = "all"
    message_view_counts: dict[str, int] = Field(default_factory=dict)
    msg_offset: int = Field(ge=0)
    msg_limit: int = Field(ge=0)
    msg_has_more: bool
    msg_has_previous: bool

@router.get("")
def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    source: Optional[str] = None,
    server: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
):
    offset = (page - 1) * page_size
    sessions, total = get_sessions(
        limit=page_size, offset=offset,
        source=source, server=server,
        date_from=date_from, date_to=date_to,
        search=search,
    )
    return {
        "sessions": sessions,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
    }

@router.get("/stats")
def session_stats():
    return get_stats()

@router.get("/{session_id}", response_model=SessionDetailResponse)
def session_detail(session_id: str,
                   msg_offset: int = Query(0, ge=0),
                   msg_limit: int = Query(DEFAULT_MESSAGE_PAGE_SIZE, ge=0, le=5000),
                   content_limit: int = Query(
                       DEFAULT_MESSAGE_PREVIEW_CHARS, ge=0, le=500000
                   ),
                   anchor_message_id: Optional[int] = Query(None, ge=1),
                   message_view: str = Query("all")):
    if message_view not in MESSAGE_VIEWS:
        raise HTTPException(
            status_code=422,
            detail=f"message_view must be one of: {', '.join(MESSAGE_VIEWS)}",
        )
    detail = get_session_detail(session_id, msg_offset=msg_offset,
                                msg_limit=msg_limit,
                                content_limit=content_limit,
                                anchor_message_id=anchor_message_id,
                                message_view=message_view)
    if not detail:
        raise HTTPException(status_code=404, detail="Session not found")
    return detail


@router.get("/{session_id}/messages/{message_id}")
def message_content(session_id: str, message_id: int):
    message = get_message_content(session_id, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    return message
