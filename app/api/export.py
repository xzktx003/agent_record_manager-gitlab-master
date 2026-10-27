"""Memory-bounded export API endpoints."""
import json
from collections.abc import Iterator
from itertools import chain

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse

from app.database import get_db, get_session_detail
from app.tenant import get_data_scope, tenant_scope


router = APIRouter(prefix="/api/export", tags=["export"])
_EXPORT_BATCH_SIZE = 200
_STREAM_CHUNK_CHARS = 64 * 1024


def _tenant_stream(scope: str, chunks: Iterator[str]) -> Iterator[str]:
    """Bind lazy response iteration to the request owner's database."""
    iterator = iter(chunks)
    while True:
        # Starlette may advance a synchronous iterator in a different worker
        # context on each call. Enter and leave the tenant scope around one
        # ``next`` operation instead of holding a ContextVar token across a
        # yielded ASGI body chunk.
        with tenant_scope(scope):
            try:
                chunk = next(iterator)
            except StopIteration:
                return
        yield chunk


def _safe_filename(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in value[:80]
    ) or "session"


def _session_export_header(session_id: str) -> dict | None:
    """Fetch session metadata without materializing its full conversation."""
    detail = get_session_detail(
        session_id,
        msg_limit=1,
        content_limit=1,
    )
    if not detail:
        return None
    detail.pop("messages", None)
    total = detail["msg_total"]
    detail.update({
        "msg_offset": 0,
        "msg_limit": 0,
        "msg_has_more": False,
        "msg_has_previous": False,
        "msg_start": 1 if total else 0,
        "msg_end": total,
        "msg_page": 1,
        "msg_total_pages": 1,
        "content_limit": 0,
    })
    return detail


def _iter_session_messages(session_id: str) -> Iterator[dict]:
    """Read full messages in short keyset-paginated transactions."""
    with get_db() as conn:
        max_id = conn.execute(
            "SELECT MAX(id) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
    if max_id is None:
        return

    last_id = 0
    while last_id < max_id:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT *, length(content) AS content_length,
                          0 AS content_truncated
                   FROM messages
                   WHERE session_id = ? AND id > ? AND id <= ?
                   ORDER BY id
                   LIMIT ?""",
                (session_id, last_id, max_id, _EXPORT_BATCH_SIZE),
            ).fetchall()
        if not rows:
            return
        for row in rows:
            yield dict(row)
        last_id = rows[-1]["id"]


def _iter_comma_separated(encoded_items: Iterator[str]) -> Iterator[str]:
    """Coalesce small JSON values to avoid one ASGI send per tiny message."""
    parts = []
    buffered_chars = 0
    first = True
    for encoded in encoded_items:
        piece = encoded if first else "," + encoded
        first = False
        if parts and buffered_chars + len(piece) > _STREAM_CHUNK_CHARS:
            yield "".join(parts)
            parts = []
            buffered_chars = 0
        if len(piece) >= _STREAM_CHUNK_CHARS:
            yield piece
        else:
            parts.append(piece)
            buffered_chars += len(piece)
    if parts:
        yield "".join(parts)


def _iter_session_json(session_id: str, header: dict) -> Iterator[str]:
    encoded_header = json.dumps(
        header,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    yield encoded_header[:-1] + ',"messages":['
    encoded_messages = (
        json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for message in _iter_session_messages(session_id)
    )
    yield from _iter_comma_separated(encoded_messages)
    yield "]}"


def _iter_session_markdown(session_id: str, header: dict) -> Iterator[str]:
    yield (
        f"# {header.get('title') or 'Untitled Session'}\n\n"
        f"- **Source**: {header.get('source')}\n"
        f"- **Server**: {header.get('server_name') or 'local'}\n"
        f"- **Time**: {header.get('start_time') or 'N/A'}\n"
        f"- **CWD**: {header.get('cwd') or 'N/A'}\n\n---\n\n"
    )
    for message in _iter_session_messages(session_id):
        role = message.get("role", "unknown")
        content = message.get("content", "")
        if role == "tool":
            yield (
                "<details><summary>🔧 Tool Output</summary>\n\n"
                f"```\n{content}\n```\n</details>\n\n"
            )
        elif role == "user":
            yield f"## 👤 User\n\n{content}\n\n"
        elif role == "assistant":
            if message.get("msg_type") in {"tool_call", "function_call"}:
                yield f"🛠️ {content}\n\n"
            else:
                yield f"## 🤖 Assistant\n\n{content}\n\n"
        else:
            yield f"*[{role}]* {content}\n\n"


def _iter_openai_object(
    session_id: str,
    *,
    skip_empty: bool = False,
) -> Iterator[str]:
    messages = (
        message
        for message in _iter_session_messages(session_id)
        if message.get("role") in {"user", "assistant"}
        and message.get("content")
    )
    try:
        first_message = next(messages)
    except StopIteration:
        if not skip_empty:
            yield '{"messages":[]}'
        return

    yield '{"messages":['
    encoded_messages = (
        json.dumps(
            {
                "role": message["role"],
                "content": message["content"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for message in chain((first_message,), messages)
    )
    yield from _iter_comma_separated(encoded_messages)
    yield "]}"


@router.get("/session/{session_id}")
def export_session(
    session_id: str,
    format: str = Query("json", pattern="^(json|markdown|openai)$"),
):
    scope = get_data_scope()
    header = _session_export_header(session_id)
    if not header:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    safe_id = _safe_filename(session_id)
    if format == "json":
        return StreamingResponse(
            _tenant_stream(scope, _iter_session_json(session_id, header)),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_id}.json"'
            },
        )
    if format == "markdown":
        return StreamingResponse(
            _tenant_stream(scope, _iter_session_markdown(session_id, header)),
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_id}.md"'
            },
        )
    return StreamingResponse(
        _tenant_stream(scope, _iter_openai_object(session_id)),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{safe_id}_openai.json"'
            )
        },
    )


def _session_ids() -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM sessions ORDER BY start_time"
        ).fetchall()
    return [row["id"] for row in rows]


def _iter_all_json(session_ids: list[str]) -> Iterator[str]:
    yield "["
    first = True
    for session_id in session_ids:
        header = _session_export_header(session_id)
        if not header:
            continue
        if not first:
            yield ","
        first = False
        yield from _iter_session_json(session_id, header)
    yield "]"


def _iter_all_openai_jsonl(session_ids: list[str]) -> Iterator[str]:
    first = True
    for session_id in session_ids:
        chunks = _iter_openai_object(session_id, skip_empty=True)
        try:
            first_chunk = next(chunks)
        except StopIteration:
            continue
        if not first:
            yield "\n"
        first = False
        yield first_chunk
        yield from chunks


@router.get("/all")
def export_all(
    format: str = Query("json", pattern="^(json|openai_jsonl)$"),
):
    """Stream all sessions without retaining all message content in RAM."""
    scope = get_data_scope()
    session_ids = _session_ids()
    if format == "json":
        return StreamingResponse(
            _tenant_stream(scope, _iter_all_json(session_ids)),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    'attachment; filename="all_sessions.json"'
                )
            },
        )
    return StreamingResponse(
        _tenant_stream(scope, _iter_all_openai_jsonl(session_ids)),
        media_type="application/jsonl",
        headers={
            "Content-Disposition": (
                'attachment; filename="all_sessions.jsonl"'
            )
        },
    )
