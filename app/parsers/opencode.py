"""Read-only parser for current and legacy OpenCode session storage."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


MAX_CONTENT_LEN = 500000
PARSER_VERSION = "opencode-parser-v1"
_REQUIRED_TABLE_COLUMNS = {
    "session": {"id", "directory", "title", "time_created", "time_updated"},
    "message": {"id", "session_id", "time_created", "time_updated", "data"},
    "part": {
        "id", "message_id", "session_id", "time_created", "time_updated", "data",
    },
}
_SKIPPED_PART_TYPES = {"step-start", "step-finish"}


class OpenCodeFormatError(RuntimeError):
    """The configured OpenCode storage exists but has an unsupported schema."""


def resolve_opencode_path(configured_path: str = "") -> str:
    """Resolve OpenCode data using explicit config, env overrides, then XDG."""
    if configured_path:
        return os.path.abspath(os.path.expanduser(configured_path))
    if os.environ.get("OPENCODE_DB_PATH"):
        return os.path.abspath(os.path.expanduser(os.environ["OPENCODE_DB_PATH"]))
    if os.environ.get("OPENCODE_HOME"):
        return os.path.abspath(os.path.expanduser(os.environ["OPENCODE_HOME"]))
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.abspath(os.path.join(data_home, "opencode"))


def _truncate(value: str) -> str:
    if len(value) <= MAX_CONTENT_LEN:
        return value
    return value[:MAX_CONTENT_LEN] + "...[truncated]"


def _json_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _millis_to_iso(value) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return value
    elif isinstance(value, (int, float)):
        numeric = float(value)
    else:
        return None
    if numeric > 10_000_000_000:
        numeric /= 1000.0
    try:
        return (
            datetime.fromtimestamp(numeric, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _time_value(data: dict, *keys: str):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _connect_readonly(database_path: str) -> sqlite3.Connection:
    try:
        uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (OSError, sqlite3.Error) as exc:
        raise OpenCodeFormatError(f"无法只读打开 OpenCode 数据库: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _sqlite_schema(conn: sqlite3.Connection) -> tuple[dict[str, set[str]], str]:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    columns: dict[str, set[str]] = {}
    missing = []
    for table, required in _REQUIRED_TABLE_COLUMNS.items():
        if table not in tables:
            missing.append(f"table:{table}")
            continue
        columns[table] = _table_columns(conn, table)
        for column in sorted(required - columns[table]):
            missing.append(f"{table}.{column}")
    if missing:
        raise OpenCodeFormatError(
            "OpenCode SQLite schema 不兼容，缺少 " + ", ".join(missing)
        )
    if "project" in tables:
        columns["project"] = _table_columns(conn, "project")
    normalized = {
        table: sorted(values) for table, values in sorted(columns.items())
    }
    fingerprint = hashlib.sha256(
        json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return columns, fingerprint


def _database_candidate(path: str) -> str:
    if os.path.isfile(path):
        return path
    return os.path.join(path, "opencode.db")


def _legacy_storage_root(path: str) -> str:
    if os.path.basename(path.rstrip(os.sep)) == "storage":
        return path
    return os.path.join(path, "storage")


def discover_opencode_backend(configured_path: str = "") -> Optional[dict]:
    """Prefer a valid current SQLite database, then legacy JSON storage."""
    source_path = resolve_opencode_path(configured_path)
    database_path = _database_candidate(source_path)
    sqlite_error = None
    if os.path.isfile(database_path):
        try:
            with closing(_connect_readonly(database_path)) as conn:
                _columns, schema_fingerprint = _sqlite_schema(conn)
            return {
                "kind": "sqlite",
                "path": database_path,
                "root": os.path.dirname(database_path),
                "schema_fingerprint": schema_fingerprint,
            }
        except OpenCodeFormatError as exc:
            sqlite_error = exc

    storage_root = _legacy_storage_root(source_path)
    if os.path.isdir(os.path.join(storage_root, "session")):
        return {
            "kind": "json",
            "path": storage_root,
            "root": os.path.dirname(storage_root),
            "schema_fingerprint": "legacy-json",
        }
    if sqlite_error:
        raise sqlite_error
    return None


def _node_signature(item: dict, schema_fingerprint: str) -> str:
    payload = {
        "parser": PARSER_VERSION,
        "schema": schema_fingerprint,
        "id": item.get("session_id"),
        "parent_id": item.get("parent_id"),
        "session_updated": int(item.get("session_updated") or 0),
        "message_count": int(item.get("message_count") or 0),
        "part_count": int(item.get("part_count") or 0),
        "message_updated": int(item.get("message_updated") or 0),
        "part_updated": int(item.get("part_updated") or 0),
        "files": item.get("files") or [],
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _group_descriptors(items: list[dict], schema_fingerprint: str) -> list[dict]:
    by_id = {item["session_id"]: item for item in items}

    def root_id(item: dict) -> str:
        current = item
        seen = {current["session_id"]}
        while current.get("parent_id") in by_id:
            parent_id = current["parent_id"]
            if parent_id in seen:
                break
            seen.add(parent_id)
            current = by_id[parent_id]
        return current["session_id"]

    groups: dict[str, list[dict]] = {}
    for item in items:
        item["node_signature"] = _node_signature(item, schema_fingerprint)
        groups.setdefault(root_id(item), []).append(item)

    result = []
    for root_session_id, members in groups.items():
        root = by_id[root_session_id]
        members.sort(key=lambda value: (int(value.get("session_created") or 0), value["session_id"]))
        signature_payload = {
            "parser": PARSER_VERSION,
            "schema": schema_fingerprint,
            "root": root_session_id,
            "members": [
                [member["session_id"], member["node_signature"]]
                for member in members
            ],
        }
        result.append({
            "session_id": root_session_id,
            "parent_id": root.get("parent_id"),
            "title": root.get("title") or "",
            "directory": root.get("directory") or "",
            "session_created": root.get("session_created"),
            "session_updated": max(
                int(member.get("session_updated") or 0) for member in members
            ),
            "member_ids": [member["session_id"] for member in members],
            "source_signature": hashlib.sha256(
                json.dumps(
                    signature_payload, separators=(",", ":"), sort_keys=True
                ).encode()
            ).hexdigest(),
        })
    result.sort(
        key=lambda value: (int(value.get("session_updated") or 0), value["session_id"]),
        reverse=True,
    )
    return result


def _scan_sqlite(backend: dict) -> list[dict]:
    with closing(_connect_readonly(backend["path"])) as conn:
        columns, _schema_fingerprint = _sqlite_schema(conn)
        conn.execute("BEGIN")
        parent_expr = "s.parent_id" if "parent_id" in columns["session"] else "NULL"
        rows = conn.execute(f"""
            WITH message_stats AS (
                SELECT session_id, COUNT(*) AS message_count,
                       MAX(time_updated) AS message_updated
                FROM message GROUP BY session_id
            ),
            part_stats AS (
                SELECT session_id, COUNT(*) AS part_count,
                       MAX(time_updated) AS part_updated
                FROM part GROUP BY session_id
            )
            SELECT s.id AS session_id, {parent_expr} AS parent_id,
                   s.title, s.directory,
                   s.time_created AS session_created,
                   s.time_updated AS session_updated,
                   COALESCE(ms.message_count, 0) AS message_count,
                   COALESCE(ps.part_count, 0) AS part_count,
                   COALESCE(ms.message_updated, 0) AS message_updated,
                   COALESCE(ps.part_updated, 0) AS part_updated
            FROM session s
            LEFT JOIN message_stats ms ON ms.session_id=s.id
            LEFT JOIN part_stats ps ON ps.session_id=s.id
        """).fetchall()
        return [dict(row) for row in rows]


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _legacy_session_files(storage_root: str) -> list[str]:
    session_root = os.path.join(storage_root, "session")
    candidates = glob.glob(os.path.join(session_root, "**", "*.json"), recursive=True)
    result = []
    for path in candidates:
        relative_parts = os.path.relpath(path, session_root).split(os.sep)
        if any(part in {"message", "part"} for part in relative_parts[:-1]):
            continue
        data = _read_json(path)
        session_id = data.get("id") or ""
        if session_id.startswith("ses_"):
            result.append(path)
    return sorted(result)


def _legacy_message_files(storage_root: str, session_id: str) -> list[str]:
    roots = [
        os.path.join(storage_root, "message", session_id),
        os.path.join(storage_root, "session", "message", session_id),
    ]
    result = []
    for root in roots:
        if os.path.isdir(root):
            result.extend(glob.glob(os.path.join(root, "*.json")))
    return sorted(set(result))


def _legacy_part_files(storage_root: str, session_id: str,
                       message_id: str) -> list[str]:
    roots = [
        os.path.join(storage_root, "part", message_id),
        os.path.join(storage_root, "session", "part", session_id, message_id),
    ]
    result = []
    for root in roots:
        if os.path.isdir(root):
            result.extend(glob.glob(os.path.join(root, "*.json")))
    return sorted(set(result))


def _file_fingerprint(path: str) -> list:
    try:
        file_stat = os.stat(path)
    except OSError:
        return [path, 0, 0]
    return [path, int(file_stat.st_mtime_ns), int(file_stat.st_size)]


def _legacy_time(data: dict, key: str):
    time_data = data.get("time") if isinstance(data.get("time"), dict) else {}
    return time_data.get(key) or data.get(f"time_{key}")


def _scan_json(backend: dict) -> list[dict]:
    items = []
    for session_file in _legacy_session_files(backend["path"]):
        session = _read_json(session_file)
        session_id = session.get("id")
        if not session_id:
            continue
        files = [_file_fingerprint(session_file)]
        message_files = _legacy_message_files(backend["path"], session_id)
        message_updated = 0
        part_updated = 0
        part_count = 0
        for message_file in message_files:
            message = _read_json(message_file)
            message_updated = max(
                message_updated, int(_legacy_time(message, "updated") or 0)
            )
            files.append(_file_fingerprint(message_file))
            for part_file in _legacy_part_files(
                backend["path"], session_id, message.get("id") or ""
            ):
                part = _read_json(part_file)
                part_updated = max(
                    part_updated, int(_legacy_time(part, "updated") or 0)
                )
                part_count += 1
                files.append(_file_fingerprint(part_file))
        items.append({
            "session_id": session_id,
            "parent_id": session.get("parentID") or session.get("parent_id"),
            "title": session.get("title") or "",
            "directory": session.get("directory") or "",
            "session_created": _legacy_time(session, "created") or 0,
            "session_updated": _legacy_time(session, "updated") or 0,
            "message_count": len(message_files),
            "part_count": part_count,
            "message_updated": message_updated,
            "part_updated": part_updated,
            "files": sorted(files),
        })
    return items


def scan_opencode_source(configured_path: str = "") -> Optional[dict]:
    """Return lightweight root-session groups and stable change signatures."""
    backend = discover_opencode_backend(configured_path)
    if not backend:
        return None
    items = _scan_sqlite(backend) if backend["kind"] == "sqlite" else _scan_json(backend)
    return {
        **backend,
        "groups": _group_descriptors(items, backend["schema_fingerprint"]),
    }


def _metadata_base(message_row: dict, part_row: dict, part: dict) -> dict:
    metadata = {
        "opencode_message_id": message_row.get("id"),
        "opencode_part_id": part_row.get("id"),
        "opencode_part_type": part.get("type") or "unknown",
    }
    for key in ("synthetic", "ignored"):
        if key in part:
            metadata[key] = part[key]
    return metadata


def _append_message(messages: list[dict], role: str, content: str,
                    msg_type: str, timestamp: Optional[str], metadata: dict,
                    order: tuple):
    if not content:
        return
    messages.append({
        "role": role,
        "content": _truncate(content),
        "msg_type": msg_type,
        "timestamp": timestamp,
        "metadata": metadata,
        "_order": order,
    })


def _attachment_metadata(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:50]:
        if not isinstance(item, dict):
            continue
        result.append({
            key: item.get(key)
            for key in ("filename", "mime")
            if item.get(key) not in (None, "")
        })
    return [item for item in result if item]


def _normalize_part(messages: list[dict], role: str, message_row: dict,
                    part_row: dict, part: dict, message_index: int,
                    part_index: int):
    part_type = str(part.get("type") or "unknown")
    if part_type in _SKIPPED_PART_TYPES:
        return
    metadata = _metadata_base(message_row, part_row, part)
    base_timestamp = _millis_to_iso(part_row.get("time_created"))
    order = (message_index, part_index, 0)

    if part_type == "text":
        text_role = "system" if part.get("synthetic") else role
        msg_type = (
            "synthetic_context"
            if part.get("synthetic")
            else ("input_text" if role == "user" else "agent_message")
        )
        _append_message(
            messages, text_role, str(part.get("text") or ""), msg_type,
            _millis_to_iso(_time_value(part, "time", "start")) or base_timestamp,
            metadata, order,
        )
        return

    if part_type == "reasoning":
        text = str(part.get("text") or "")
        _append_message(
            messages, "assistant", f"[Reasoning] {text}" if text else "",
            "reasoning", base_timestamp, metadata, order,
        )
        return

    if part_type == "tool":
        tool_name = str(part.get("tool") or "unknown")
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = str(state.get("status") or "unknown")
        call_id = part.get("callID") or part.get("call_id")
        tool_metadata = {
            **metadata,
            "tool_name": tool_name,
            "call_id": call_id,
            "status": status,
        }
        input_text = _json_text(state.get("input"))
        call_content = f"[Tool: {tool_name}]"
        if input_text:
            call_content += " " + input_text
        _append_message(
            messages, "assistant", call_content, "tool_call",
            _millis_to_iso(_time_value(state, "time", "start")) or base_timestamp,
            tool_metadata, order,
        )

        attachments = _attachment_metadata(state.get("attachments"))
        output = _json_text(state.get("output"))
        error = _json_text(state.get("error"))
        state_metadata = (
            state.get("metadata")
            if isinstance(state.get("metadata"), dict) else {}
        )
        child_session_id = (
            state_metadata.get("sessionId")
            or state_metadata.get("sessionID")
            or state_metadata.get("session_id")
        )
        if child_session_id:
            tool_metadata["child_session_id"] = child_session_id
        if attachments:
            suffix = "\n[Attachments] " + _json_text(attachments)
            output = (output + suffix).strip()
        if status in {"completed", "error", "failed"} and (output or error):
            if error:
                content = f"[Error] {error}"
                if output:
                    content += "\n" + output
                result_type = "tool_error"
            else:
                content = output
                result_type = "tool_result"
            result_metadata = dict(tool_metadata)
            if attachments:
                result_metadata["attachments"] = attachments
            _append_message(
                messages, "tool", content, result_type,
                _millis_to_iso(_time_value(state, "time", "end"))
                or _millis_to_iso(part_row.get("time_updated"))
                or base_timestamp,
                result_metadata, (message_index, part_index, 1),
            )
        return

    if part_type == "patch":
        files = part.get("files") if isinstance(part.get("files"), list) else []
        content = "[Patch]"
        if files:
            content += "\n" + "\n".join(str(item) for item in files)
        elif part.get("hash"):
            content += f" {part['hash']}"
        _append_message(
            messages, "tool", content, "patch_apply", base_timestamp,
            {**metadata, "hash": part.get("hash"), "files": files[:200]}, order,
        )
        return

    if part_type == "file":
        filename = part.get("filename") or part.get("name") or "attachment"
        mime = part.get("mime") or ""
        source = part.get("source") if isinstance(part.get("source"), dict) else {}
        content = f"[File: {filename}]"
        if mime:
            content += f" {mime}"
        _append_message(
            messages, role, content, "file", base_timestamp,
            {**metadata, "filename": filename, "mime": mime, "source": source}, order,
        )
        return

    if part_type == "compaction":
        content = "[Compaction] Conversation context was compacted."
        _append_message(
            messages, "system", content, "compaction", base_timestamp,
            {**metadata, **{key: part.get(key) for key in ("auto", "overflow", "tail_start_id")}},
            order,
        )
        return

    if part_type == "subtask":
        prompt = part.get("prompt") or part.get("description") or ""
        agent = part.get("agent") or part.get("agentName") or "subagent"
        content = f"[Subtask: {agent}]"
        if prompt:
            content += " " + str(prompt)
        _append_message(
            messages, "assistant", content, "subtask", base_timestamp,
            {**metadata, "agent": agent}, order,
        )
        return

    raw = _json_text(part)
    _append_message(
        messages, "system", f"[OpenCode {part_type}] {raw}",
        f"opencode_{part_type}", base_timestamp, metadata, order,
    )


def _message_fallback(messages: list[dict], message_row: dict, message: dict,
                      message_index: int, before_count: int):
    if len(messages) != before_count:
        return
    role = message.get("role") or "unknown"
    timestamp = _millis_to_iso(message_row.get("time_created"))
    metadata = {"opencode_message_id": message_row.get("id")}
    if message.get("error"):
        _append_message(
            messages, "assistant", "[Error] " + _json_text(message.get("error")),
            "error", timestamp, metadata, (message_index, 0, 0),
        )
        return
    summary = message.get("summary")
    if isinstance(summary, dict):
        text = summary.get("title") if role == "user" else summary.get("body")
        text = text or summary.get("body") or summary.get("title")
        _append_message(
            messages, role, str(text or ""), "summary", timestamp,
            metadata, (message_index, 0, 0),
        )


def _model_details(session_row: dict, message_rows: list[dict]) -> tuple[str, str]:
    session_model = _json_object(session_row.get("model"))
    provider_id = session_model.get("providerID") or session_model.get("provider_id") or ""
    model_id = session_model.get("id") or session_model.get("modelID") or ""
    if not model_id:
        for row in message_rows:
            message = _json_object(row.get("data"))
            if message.get("role") != "assistant":
                continue
            provider_id = message.get("providerID") or provider_id
            model_id = message.get("modelID") or model_id
            if model_id:
                break
    return (
        f"{provider_id}/{model_id}" if provider_id and model_id else str(model_id or provider_id),
        str(provider_id),
    )


def _normalize_session(session_row: dict, message_rows: list[dict],
                       parts_by_message: dict[str, list[dict]], server_name: str,
                       raw_path: str, backend_kind: str) -> dict:
    upstream_id = session_row["id"]
    model, provider_id = _model_details(session_row, message_rows)
    metadata = {
        "opencode_session_id": upstream_id,
        "project_id": session_row.get("project_id"),
        "project_name": session_row.get("project_name"),
        "project_worktree": session_row.get("project_worktree"),
        "parent_session_id": session_row.get("parent_id"),
        "slug": session_row.get("slug"),
        "workspace_id": session_row.get("workspace_id"),
        "agent": session_row.get("agent"),
        "provider_id": provider_id,
        "archived_at": _millis_to_iso(session_row.get("time_archived")),
        "source_backend": backend_kind,
    }
    for key in (
        "cost", "tokens_input", "tokens_output", "tokens_reasoning",
        "tokens_cache_read", "tokens_cache_write",
    ):
        if session_row.get(key) not in (None, ""):
            metadata[key] = session_row.get(key)
    session_metadata = _json_object(session_row.get("metadata"))
    if session_metadata:
        metadata["opencode_metadata"] = session_metadata

    messages = []
    for message_index, message_row in enumerate(message_rows):
        message = _json_object(message_row.get("data"))
        role = str(message.get("role") or "unknown")
        before_count = len(messages)
        for part_index, part_row in enumerate(parts_by_message.get(message_row["id"], [])):
            part = _json_object(part_row.get("data"))
            _normalize_part(
                messages, role, message_row, part_row, part,
                message_index, part_index,
            )
        _message_fallback(messages, message_row, message, message_index, before_count)

    messages.sort(key=lambda item: (item.get("timestamp") or "", item["_order"]))
    for message in messages:
        message.pop("_order", None)
    session = {
        "id": f"opencode-{server_name}-{upstream_id}",
        "source": "opencode",
        "server_name": server_name,
        "title": session_row.get("title") or "",
        "cwd": session_row.get("directory") or session_row.get("project_worktree"),
        "model": model,
        "cli_version": session_row.get("version"),
        "start_time": _millis_to_iso(session_row.get("time_created")),
        "end_time": _millis_to_iso(session_row.get("time_updated")),
        "message_count": len(messages),
        "metadata": metadata,
        "raw_path": raw_path,
    }
    return {"session": session, "messages": messages, "_upstream_id": upstream_id}


def _chunks(values: list[str], size: int = 400):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _load_sqlite_members(source: dict, member_ids: list[str],
                         server_name: str) -> dict[str, dict]:
    with closing(_connect_readonly(source["path"])) as conn:
        columns, _schema_fingerprint = _sqlite_schema(conn)
        conn.execute("BEGIN")
        sessions = {}
        project_join = (
            "LEFT JOIN project p ON p.id=s.project_id"
            if "project" in columns and "project_id" in columns["session"]
            else ""
        )
        project_name = "p.name" if project_join and "name" in columns["project"] else "NULL"
        project_worktree = (
            "p.worktree" if project_join and "worktree" in columns["project"] else "NULL"
        )
        for chunk in _chunks(member_ids):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(f"""
                SELECT s.*, {project_name} AS project_name,
                       {project_worktree} AS project_worktree
                FROM session s {project_join}
                WHERE s.id IN ({placeholders})
            """, chunk).fetchall()
            sessions.update({row["id"]: dict(row) for row in rows})

        messages_by_session: dict[str, list[dict]] = {sid: [] for sid in member_ids}
        parts_by_message: dict[str, list[dict]] = {}
        for chunk in _chunks(member_ids):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(f"""
                SELECT * FROM message WHERE session_id IN ({placeholders})
                ORDER BY time_created, id
            """, chunk).fetchall()
            for row in rows:
                value = dict(row)
                messages_by_session.setdefault(value["session_id"], []).append(value)
            part_rows = conn.execute(f"""
                SELECT * FROM part WHERE session_id IN ({placeholders})
                ORDER BY time_created, id
            """, chunk).fetchall()
            for row in part_rows:
                value = dict(row)
                parts_by_message.setdefault(value["message_id"], []).append(value)

        return {
            session_id: _normalize_session(
                session_row,
                messages_by_session.get(session_id, []),
                parts_by_message,
                server_name,
                source.get("origin_path") or source["path"],
                "sqlite",
            )
            for session_id, session_row in sessions.items()
        }


def _legacy_session_row(data: dict) -> dict:
    model = data.get("model")
    return {
        "id": data.get("id"),
        "project_id": data.get("projectID") or data.get("project_id"),
        "workspace_id": data.get("workspaceID") or data.get("workspace_id"),
        "parent_id": data.get("parentID") or data.get("parent_id"),
        "slug": data.get("slug"),
        "directory": data.get("directory") or _time_value(data, "path", "root"),
        "title": data.get("title") or "",
        "version": data.get("version"),
        "agent": data.get("agent"),
        "model": _json_text(model) if isinstance(model, dict) else model,
        "metadata": _json_text(data.get("metadata")) if data.get("metadata") else None,
        "time_created": _legacy_time(data, "created"),
        "time_updated": _legacy_time(data, "updated"),
        "time_archived": _legacy_time(data, "archived"),
        "project_name": None,
        "project_worktree": None,
    }


def _load_json_members(source: dict, member_ids: list[str],
                       server_name: str) -> dict[str, dict]:
    wanted = set(member_ids)
    result = {}
    for session_file in _legacy_session_files(source["path"]):
        session_data = _read_json(session_file)
        session_id = session_data.get("id")
        if session_id not in wanted:
            continue
        message_rows = []
        parts_by_message: dict[str, list[dict]] = {}
        for message_file in _legacy_message_files(source["path"], session_id):
            message = _read_json(message_file)
            message_id = message.get("id") or os.path.splitext(os.path.basename(message_file))[0]
            message_rows.append({
                "id": message_id,
                "session_id": session_id,
                "time_created": _legacy_time(message, "created"),
                "time_updated": _legacy_time(message, "updated") or _legacy_time(message, "created"),
                "data": _json_text(message),
            })
            for part_file in _legacy_part_files(source["path"], session_id, message_id):
                part = _read_json(part_file)
                parts_by_message.setdefault(message_id, []).append({
                    "id": part.get("id") or os.path.splitext(os.path.basename(part_file))[0],
                    "message_id": message_id,
                    "session_id": session_id,
                    "time_created": _legacy_time(part, "created") or _legacy_time(message, "created"),
                    "time_updated": _legacy_time(part, "updated") or _legacy_time(message, "updated"),
                    "data": _json_text(part),
                })
        message_rows.sort(key=lambda row: (int(row.get("time_created") or 0), row["id"]))
        for values in parts_by_message.values():
            values.sort(key=lambda row: (int(row.get("time_created") or 0), row["id"]))
        result[session_id] = _normalize_session(
            _legacy_session_row(session_data), message_rows, parts_by_message,
            server_name, session_file, "json",
        )
    return result


def _child_label(result: dict) -> str:
    metadata = result["session"].get("metadata") or {}
    agent = metadata.get("agent") or metadata.get("slug")
    if not agent:
        agent = result["session"].get("title") or result.get("_upstream_id") or "subagent"
    label = " ".join(str(agent).split())[:80]
    return f"[subagent:{label}]"


def _prune_embedded_child_results(messages: list[dict], child_ids: set[str]) -> None:
    if not child_ids:
        return
    for message in messages:
        metadata = message.get("metadata") or {}
        child_id = metadata.get("child_session_id")
        if (
            child_id not in child_ids
            or message.get("role") != "tool"
            or metadata.get("status") != "completed"
        ):
            continue
        message["content"] = f"[Subagent session: {child_id}] completed"
        message["msg_type"] = "subagent_result"


def _merge_group(root_id: str, member_ids: list[str],
                 parsed: dict[str, dict]) -> Optional[dict]:
    root = parsed.get(root_id)
    if not root:
        return None
    _prune_embedded_child_results(root["messages"], set(member_ids) - {root_id})
    merged_children = []
    for child_id in member_ids:
        if child_id == root_id or child_id not in parsed:
            continue
        child = parsed[child_id]
        label = _child_label(child)
        child_session = child["session"]
        merged_children.append({
            "session_id": child_id,
            "parent_session_id": (child_session.get("metadata") or {}).get("parent_session_id"),
            "title": child_session.get("title"),
            "agent": (child_session.get("metadata") or {}).get("agent"),
            "label": label,
        })
        root["messages"].append({
            "role": "system",
            "content": f"--- OpenCode subagent {label} ---",
            "msg_type": "subagent_start",
            "timestamp": child_session.get("start_time"),
            "metadata": {
                "subagent": label,
                "merged_from_session_id": child_id,
            },
        })
        for message in child.get("messages", []):
            merged = dict(message)
            metadata = dict(merged.get("metadata") or {})
            metadata["subagent"] = label
            metadata["merged_from_session_id"] = child_id
            merged["metadata"] = metadata
            if merged.get("role") == "assistant":
                merged["content"] = f"{label} {merged.get('content', '')}".strip()
            root["messages"].append(merged)
        child_end = child_session.get("end_time")
        root_end = root["session"].get("end_time")
        if child_end and (not root_end or child_end > root_end):
            root["session"]["end_time"] = child_end

    root["messages"] = [
        message for _, message in sorted(
            enumerate(root["messages"]),
            key=lambda item: (item[1].get("timestamp") or "", item[0]),
        )
    ]
    root["session"]["message_count"] = len(root["messages"])
    if merged_children:
        root["session"].setdefault("metadata", {})["merged_child_sessions"] = merged_children
    root.pop("_upstream_id", None)
    return root


def parse_opencode_groups(source: dict, server_name: str = "local",
                          root_ids: Optional[list[str]] = None) -> list[dict]:
    """Hydrate selected root sessions and merge their child/subagent sessions."""
    selected_groups = [
        group for group in source.get("groups", [])
        if root_ids is None or group["session_id"] in set(root_ids)
    ]
    member_ids = sorted({
        session_id
        for group in selected_groups
        for session_id in group.get("member_ids", [])
    })
    if not member_ids:
        return []
    if source["kind"] == "sqlite":
        parsed = _load_sqlite_members(source, member_ids, server_name)
    else:
        parsed = _load_json_members(source, member_ids, server_name)
    results = []
    for group in selected_groups:
        result = _merge_group(group["session_id"], group["member_ids"], parsed)
        if result:
            results.append(result)
    return results
