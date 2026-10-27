"""Full-text search API with enhanced trigram search."""
from typing import Optional

from fastapi import APIRouter, Query, Response
from app.database import (
    MIN_INDEXED_QUERY_CHARS,
    is_indexed_content_query,
    search_sessions,
)

router = APIRouter(prefix="/api/search", tags=["search"])

@router.get("")
def search(
    response: Response,
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: Optional[str] = None,
    server: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    compact: bool = False,
    match_mode: str = Query("expanded", pattern="^(expanded|precise)$"),
):
    response.headers["Cache-Control"] = "no-store"
    q = q.strip()
    results, total = search_sessions(
        query=q, limit=limit, offset=offset,
        source=source, server=server,
        date_from=date_from, date_to=date_to,
        match_mode=match_mode,
    )
    if compact:
        # Search cards navigate to the canonical session and render `snippet`;
        # they do not need every result's full 16 KiB message preview.
        results = [
            {**row, "content": (row.get("content") or "")[:800]}
            for row in results
        ]
    return {
        "results": results,
        "query": q,
        "count": len(results),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(results) < total,
        "result_unit": "session",
        "match_mode": match_mode,
        "scope": (
            "messages_and_session_metadata"
            if is_indexed_content_query(q)
            else "session_metadata"
        ),
        "min_content_query_chars": MIN_INDEXED_QUERY_CHARS,
    }
