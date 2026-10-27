"""Parser for Claude Code / Claude CLI project session JSONL files."""
import glob
import json
import os
from typing import Optional

MAX_CONTENT_LEN = 500000
_SKIPPED_EVENT_TYPES = {"attachment", "hook", "permission", "permission_snapshot"}
_SKIPPED_PART_TYPES = {"attachment", "image", "image_url", "document"}


def _truncate(text: str) -> str:
    if len(text) > MAX_CONTENT_LEN:
        return text[:MAX_CONTENT_LEN] + "...[truncated]"
    return text


def _json_string(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _content_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") in _SKIPPED_PART_TYPES:
                    continue
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif item is not None:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return _json_string(value)


def _append_message(messages: list[dict], role: str, content: str, msg_type: str,
                    timestamp: Optional[str], metadata: Optional[dict] = None):
    if not content:
        return
    msg = {
        "role": role,
        "content": _truncate(content),
        "msg_type": msg_type,
        "timestamp": timestamp,
    }
    if metadata:
        msg["metadata"] = metadata
    messages.append(msg)


def _append_content_messages(messages: list[dict], role: str, content, event_type: str,
                             timestamp: Optional[str], uuid: Optional[str]) -> list[str]:
    normal_texts = []
    metadata = {"uuid": uuid} if uuid else None

    if isinstance(content, str):
        normal_texts.append(content)
    elif isinstance(content, list):
        text_parts = []
        for item in content:
            if not isinstance(item, dict):
                if item is not None:
                    text_parts.append(str(item))
                continue

            part_type = item.get("type") or ""
            if part_type in _SKIPPED_PART_TYPES:
                continue
            if part_type == "tool_use":
                tool_name = item.get("name") or "unknown"
                tool_input = _json_string(item.get("input"))
                tool_meta = {"tool_name": tool_name}
                if item.get("id"):
                    tool_meta["tool_id"] = item.get("id")
                if uuid:
                    tool_meta["uuid"] = uuid
                _append_message(
                    messages,
                    "assistant",
                    f"[Tool: {tool_name}] {tool_input}".strip(),
                    "tool_use",
                    timestamp,
                    tool_meta,
                )
                continue
            if part_type == "tool_result":
                tool_meta = {}
                if item.get("tool_use_id"):
                    tool_meta["tool_use_id"] = item.get("tool_use_id")
                if "is_error" in item:
                    tool_meta["is_error"] = item.get("is_error")
                if uuid:
                    tool_meta["uuid"] = uuid
                _append_message(
                    messages,
                    "tool",
                    _content_text(item.get("content")),
                    "tool_result",
                    timestamp,
                    tool_meta,
                )
                continue

            if isinstance(item.get("text"), str):
                text_parts.append(item["text"])
            elif isinstance(item.get("content"), str):
                text_parts.append(item["content"])

        if text_parts:
            normal_texts.append("\n".join(part for part in text_parts if part))
    else:
        text = _content_text(content)
        if text:
            normal_texts.append(text)

    for text in normal_texts:
        _append_message(messages, role, text, event_type, timestamp, metadata)
    return normal_texts


def find_claude_session_files(projects_dir: str) -> list[str]:
    """Find Claude project JSONL session files under ~/.claude/projects."""
    if not projects_dir or not os.path.isdir(projects_dir):
        return []
    return sorted(glob.glob(os.path.join(projects_dir, "*", "*.jsonl")))


def parse_claude_session(session_file: str, server_name: str = "local") -> Optional[dict]:
    """Parse one Claude project session JSONL file."""
    if not os.path.exists(session_file):
        return None

    session_uuid = os.path.splitext(os.path.basename(session_file))[0]
    project_dir = os.path.dirname(session_file)
    session_data = {
        "id": f"claude-{server_name}-{session_uuid}",
        "source": "claude",
        "server_name": server_name,
        "raw_path": session_file,
        "metadata": {
            "project": os.path.basename(project_dir),
            "project_dir": project_dir,
            "claude_session_id": session_uuid,
        },
    }
    messages = []
    start_time = None
    end_time = None

    try:
        with open(session_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type") or ""
                if event_type in _SKIPPED_EVENT_TYPES:
                    continue
                message = event.get("message") or {}
                role = message.get("role") or event_type
                if role not in {"user", "assistant", "system"}:
                    continue

                timestamp = event.get("timestamp")
                if timestamp:
                    if start_time is None:
                        start_time = timestamp
                    end_time = timestamp
                if event.get("cwd") and not session_data.get("cwd"):
                    session_data["cwd"] = event.get("cwd")
                if event.get("version") and not session_data.get("cli_version"):
                    session_data["cli_version"] = event.get("version")

                normal_texts = _append_content_messages(
                    messages,
                    role,
                    message.get("content"),
                    event_type,
                    timestamp,
                    event.get("uuid"),
                )
                if not session_data.get("title") and role == "user":
                    first_text = next((text for text in normal_texts if text), "")
                    if first_text:
                        session_data["title"] = first_text[:200].replace("\n", " ")
    except Exception as exc:
        session_data["metadata"]["parse_error"] = str(exc)

    if not messages:
        return None
    session_data["start_time"] = start_time
    session_data["end_time"] = end_time
    session_data["message_count"] = len(messages)
    return {"session": session_data, "messages": messages}