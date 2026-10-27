"""
Parser for VS Code Copilot Chat integration data.
Extracts:
  1. Session metadata from copilotcli.session.metadata.json
  2. Agent memory files from workspaceStorage memory-tool directories
  3. Chat-edit history from VS Code History entries.json
  4. Debug-logs from agentDebugLog.fileLogging (main.jsonl) — full conversation data
"""
import json
import os
import glob as glob_mod
import re
import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Dict


def parse_vscode_session_metadata(vscode_dir: str) -> dict:
    """Parse copilotcli.session.metadata.json to get workspace folder and firstUserMessage.

    Returns: {session_uuid: {"workspace_folder": path, "first_user_message": msg, "written_to_disc": bool}}
    """
    metadata_path = os.path.join(
        vscode_dir,
        "data/User/globalStorage/github.copilot-chat/copilotcli/copilotcli.session.metadata.json",
    )
    if not os.path.exists(metadata_path):
        return {}

    try:
        with open(metadata_path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    result = {}
    if isinstance(raw, dict):
        for session_uuid, meta in raw.items():
            if not isinstance(meta, dict):
                continue
            result[session_uuid] = _extract_meta_fields(meta)
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            sid = item.get("sessionId") or item.get("id") or item.get("session_id")
            if not sid:
                continue
            result[sid] = _extract_meta_fields(item)

    return result


def _extract_meta_fields(meta: dict) -> dict:
    """Extract workspace_folder (as string path), first_user_message, written_to_disc."""
    wf = meta.get("workspaceFolder") or meta.get("workspace_folder")
    # workspaceFolder can be a dict like {"folderPath": "/path", "timestamp": 123}
    if isinstance(wf, dict):
        wf = wf.get("folderPath") or wf.get("folder_path") or ""
    elif not isinstance(wf, str):
        wf = ""
    return {
        "workspace_folder": wf or "",
        "first_user_message": meta.get("firstUserMessage") or meta.get("first_user_message") or "",
        "written_to_disc": meta.get("writtenToDisc", meta.get("written_to_disc", False)),
    }


def parse_vscode_memory_files(vscode_dir: str, server_name: str = "local") -> list:
    """Parse agent memory files from VS Code workspace storage.

    Scans: vscode_dir/data/User/workspaceStorage/*/GitHub.copilot-chat/memory-tool/memories/**/*.md
    Returns list of session-like dicts with source='vscode-memory' for import.
    """
    pattern = os.path.join(
        vscode_dir,
        "data/User/workspaceStorage/*/GitHub.copilot-chat/memory-tool/memories/**/*.md",
    )
    files = sorted(glob_mod.glob(pattern, recursive=True))
    results = []

    for filepath in files:
        try:
            # Extract workspace hash from path
            # .../workspaceStorage/<hash>/GitHub.copilot-chat/...
            parts = filepath.replace("\\", "/").split("/")
            ws_idx = None
            for i, p in enumerate(parts):
                if p == "workspaceStorage" and i + 1 < len(parts):
                    ws_idx = i + 1
                    break
            workspace_hash = parts[ws_idx] if ws_idx else "unknown"

            filename = os.path.splitext(os.path.basename(filepath))[0]
            session_id = f"vscode-memory-{server_name}-{workspace_hash}-{filename}"

            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            if not content.strip():
                continue

            # Title from first non-empty line
            title = ""
            for line in content.splitlines():
                stripped = line.strip().lstrip("#").strip()
                if stripped:
                    title = stripped[:200]
                    break

            # Use file mtime as timestamp
            mtime = os.path.getmtime(filepath)
            ts = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

            results.append({
                "session": {
                    "id": session_id,
                    "source": "vscode-memory",
                    "server_name": server_name,
                    "title": title,
                    "cwd": None,
                    "model": None,
                    "cli_version": None,
                    "start_time": ts,
                    "end_time": ts,
                    "message_count": 1,
                    "metadata": {
                        "workspace_hash": workspace_hash,
                        "file_path": filepath,
                    },
                    "raw_path": filepath,
                },
                "messages": [
                    {
                        "role": "system",
                        "content": content,
                        "msg_type": "memory",
                        "timestamp": ts,
                    }
                ],
            })
        except Exception:
            continue

    return results


def parse_vscode_chat_edits(vscode_dir: str, server_name: str = "local") -> list:
    """Parse VS Code History entries.json files to find chat-edit activities.

    Scans: vscode_dir/data/User/History/*/entries.json
    Filters entries where "source" starts with "聊天编辑" or "Chat Edit".
    Groups edits by workspace (extracted from resource URI path).
    Returns list of session-like dicts with source='vscode-chat-edit'.
    """
    history_base = os.path.join(vscode_dir, "data/User/History")
    if not os.path.isdir(history_base):
        return []

    # Collect all chat-edit entries across all history dirs
    # Group by workspace root
    workspace_edits: Dict[str, list] = {}

    try:
        history_dirs = os.listdir(history_base)
    except OSError:
        return []

    for hdir in history_dirs:
        entries_path = os.path.join(history_base, hdir, "entries.json")
        if not os.path.isfile(entries_path):
            continue
        try:
            with open(entries_path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        resource = data.get("resource")
        entries = data.get("entries", [])
        if not resource or not entries:
            continue

        for entry in entries:
            entry_source = entry.get("source", "")
            if not entry_source:
                continue
            if not (entry_source.startswith("聊天编辑") or entry_source.startswith("Chat Edit")):
                continue

            # Extract workspace from resource URI
            # resource is typically: file:///path/to/workspace/file.py
            file_path = resource
            if file_path.startswith("file://"):
                file_path = file_path[7:]
            # Also handle vscode-remote URIs
            if "://" in file_path:
                file_path = file_path.split("://", 1)[-1]
                # Strip authority for remote URIs
                if "/" in file_path:
                    file_path = "/" + file_path.split("/", 1)[-1]

            # Guess workspace root: take first 3-4 path segments
            workspace = _extract_workspace(file_path)

            ts_ms = entry.get("timestamp")
            ts = None
            if ts_ms and isinstance(ts_ms, (int, float)):
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()

            edit_info = {
                "file_path": file_path,
                "source_label": entry_source,
                "timestamp": ts,
                "history_dir": hdir,
            }

            if workspace not in workspace_edits:
                workspace_edits[workspace] = []
            workspace_edits[workspace].append(edit_info)

    # Convert grouped edits into sessions
    results = []
    for workspace, edits in workspace_edits.items():
        edits.sort(key=lambda e: e.get("timestamp") or "")
        ws_hash = hashlib.md5(workspace.encode()).hexdigest()[:8]
        session_id = f"vscode-chat-edit-{server_name}-{ws_hash}"

        timestamps = [e["timestamp"] for e in edits if e.get("timestamp")]
        start_time = min(timestamps) if timestamps else None
        end_time = max(timestamps) if timestamps else None

        # Build summary content
        files_edited = sorted(set(e["file_path"] for e in edits))
        summary_lines = [f"VS Code Chat Edit activity in workspace: {workspace}"]
        summary_lines.append(f"Files edited ({len(files_edited)}):")
        for fp in files_edited:
            summary_lines.append(f"  - {fp}")
        summary_lines.append(f"\nEdit entries ({len(edits)}):")
        for e in edits:
            summary_lines.append(f"  [{e.get('timestamp', '?')}] {e['source_label']} → {e['file_path']}")
        content = "\n".join(summary_lines)

        title = f"Chat Edit: {workspace}" if len(workspace) <= 150 else f"Chat Edit: ...{workspace[-140:]}"

        results.append({
            "session": {
                "id": session_id,
                "source": "vscode-chat-edit",
                "server_name": server_name,
                "title": title,
                "cwd": workspace,
                "model": None,
                "cli_version": None,
                "start_time": start_time,
                "end_time": end_time,
                "message_count": len(edits),
                "metadata": {
                    "workspace": workspace,
                    "files_edited": files_edited,
                    "edit_count": len(edits),
                },
                "raw_path": os.path.join(history_base),
            },
            "messages": [
                {
                    "role": "system",
                    "content": content,
                    "msg_type": "chat_edit_summary",
                    "timestamp": start_time,
                }
            ],
        })

    return results


def _extract_workspace(file_path: str) -> str:
    """Extract likely workspace root from a file path.

    Heuristic: look for common project indicators, otherwise take the
    first 4 path segments after root.
    """
    parts = file_path.replace("\\", "/").strip("/").split("/")
    # Common project root indicators
    project_markers = {"src", "lib", "app", "pkg", "cmd", "internal", "test", "tests",
                       "node_modules", ".git", "vendor", "build", "dist", "target"}
    for i, p in enumerate(parts):
        if p in project_markers and i >= 2:
            return "/" + "/".join(parts[:i])
    # Fallback: first 4 components (e.g. /home/user/work/project)
    depth = min(4, len(parts))
    return "/" + "/".join(parts[:depth])


def _detect_workspace_from_content(content: str) -> str:
    """Try to extract workspace folder path from tool output content."""
    # Look for common path patterns in the content
    # e.g. /data01/home/user/work/project_name/...
    # Only match ASCII path characters to avoid matching CJK text
    m = re.search(r'(/(?:data\d+/)?home/[A-Za-z0-9._-]+/work/[A-Za-z0-9._-]+)', content)
    if m:
        return m.group(1)
    return ""


def _detect_workspace_from_copilot_dir(ws_hash: str, vscode_dir: str) -> str:
    """Try to detect workspace folder from files inside copilot-chat storage."""
    copilot_dir = os.path.join(
        vscode_dir, "data/User/workspaceStorage", ws_hash, "GitHub.copilot-chat"
    )
    path_re = re.compile(r'(/(?:data\d+/)?home/[A-Za-z0-9._-]+/work/[A-Za-z0-9._-]+)')

    # Try memory-tool markdown files first (most readable)
    mem_pattern = os.path.join(copilot_dir, "memory-tool", "**", "*.md")
    for md_file in glob_mod.glob(mem_pattern, recursive=True):
        try:
            with open(md_file, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(5000)
            m = path_re.search(text)
            if m:
                return m.group(1)
        except OSError:
            continue

    # Try binary DB files
    for fname in ("codebase-external.sqlite", "workspace-chunks.db"):
        fpath = os.path.join(copilot_dir, fname)
        if os.path.isfile(fpath):
            try:
                with open(fpath, "rb") as f:
                    chunk = f.read(100000)
                    text = chunk.decode("utf-8", errors="ignore")
                    m = path_re.search(text)
                    if m:
                        return m.group(1)
            except OSError:
                continue
    return ""


def parse_vscode_panel_sessions(vscode_dir: str, server_name: str = "local",
                                known_session_ids: set = None) -> list:
    """Parse VS Code Chat Panel sessions from chat-session-resources.

    These sessions only leave tool output artifacts (content.txt) on disk.
    The actual conversation text is NOT persisted by VS Code.

    Scans: vscode_dir/data/User/workspaceStorage/*/GitHub.copilot-chat/chat-session-resources/<uuid>/
    Skips sessions already tracked via session-state (events.jsonl).

    Returns list of session-like dicts with source='vscode-panel'.
    """
    pattern = os.path.join(
        vscode_dir,
        "data/User/workspaceStorage/*/GitHub.copilot-chat/chat-session-resources",
    )
    resource_dirs = sorted(glob_mod.glob(pattern))
    if not resource_dirs:
        return []

    if known_session_ids is None:
        known_session_ids = set()

    # Build workspace hash → folder path mapping
    ws_hash_to_folder = {}
    ws_storage_base = os.path.join(vscode_dir, "data/User/workspaceStorage")

    # Collect all session UUIDs across all workspace storages
    session_map = {}  # uuid -> list of (workspace_hash, tool_dir_path, mtime)
    uuid_re = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

    for res_dir in resource_dirs:
        # Extract workspace hash from path
        parts = res_dir.replace("\\", "/").split("/")
        ws_hash = ""
        for i, p in enumerate(parts):
            if p == "workspaceStorage" and i + 1 < len(parts):
                ws_hash = parts[i + 1]
                break

        try:
            for entry in os.listdir(res_dir):
                if uuid_re.match(entry) and os.path.isdir(os.path.join(res_dir, entry)):
                    session_uuid = entry
                    if session_uuid not in session_map:
                        session_map[session_uuid] = []
                    session_map[session_uuid].append((ws_hash, os.path.join(res_dir, entry)))
        except OSError:
            continue

    results = []
    for session_uuid, entries in session_map.items():
        # Skip sessions already tracked by other parsers
        # Check common ID patterns
        skip = False
        for prefix in ["copilot-local-", "copilot-", f"copilot-{server_name}-"]:
            if f"{prefix}{session_uuid}" in known_session_ids:
                skip = True
                break
        if skip:
            continue

        # Collect all tool output files across workspaces
        tool_outputs = []
        ws_hash = entries[0][0]
        for _, session_dir in entries:
            try:
                for tool_dir in os.listdir(session_dir):
                    content_path = os.path.join(session_dir, tool_dir, "content.txt")
                    if os.path.isfile(content_path):
                        try:
                            mtime = os.path.getmtime(content_path)
                            size = os.path.getsize(content_path)
                            # Extract timestamp from dir name (format: toolu_XXX__vscode-TIMESTAMP)
                            ts_match = re.search(r'__vscode-(\d+)', tool_dir)
                            call_ts = int(ts_match.group(1)) / 1000 if ts_match else mtime
                            tool_outputs.append({
                                "path": content_path,
                                "tool_dir": tool_dir,
                                "mtime": mtime,
                                "call_ts": call_ts,
                                "size": size,
                            })
                        except OSError:
                            continue
            except OSError:
                continue

        if not tool_outputs:
            continue

        tool_outputs.sort(key=lambda x: x["call_ts"])

        # Build session
        earliest = min(t["call_ts"] for t in tool_outputs)
        latest = max(t["call_ts"] for t in tool_outputs)
        start_time = datetime.fromtimestamp(earliest, tz=timezone.utc).isoformat()
        end_time = datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()

        session_id = f"vscode-panel-{server_name}-{session_uuid}"

        # Create messages from tool outputs (truncated)
        messages = []
        first_content = ""
        for i, to in enumerate(tool_outputs):
            try:
                with open(to["path"], "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(50000)  # 50KB limit per tool output
            except OSError:
                content = "(read error)"

            if i == 0:
                first_content = content

            ts = datetime.fromtimestamp(to["call_ts"], tz=timezone.utc).isoformat()
            tool_id = to["tool_dir"].split("__vscode-")[0] if "__vscode-" in to["tool_dir"] else to["tool_dir"]
            messages.append({
                "role": "tool",
                "content": content,
                "msg_type": "tool_output",
                "timestamp": ts,
                "metadata": {"tool_call_id": tool_id, "size": to["size"]},
            })

        # Detect workspace folder from tool output content or copilot-chat storage
        cwd = ""
        if ws_hash not in ws_hash_to_folder:
            # Try detecting from first tool output content
            for to in tool_outputs[:3]:
                try:
                    with open(to["path"], "r", encoding="utf-8", errors="replace") as f:
                        sample = f.read(5000)
                    cwd = _detect_workspace_from_content(sample)
                    if cwd:
                        break
                except OSError:
                    continue
            if not cwd:
                cwd = _detect_workspace_from_copilot_dir(ws_hash, vscode_dir)
            ws_hash_to_folder[ws_hash] = cwd
        else:
            cwd = ws_hash_to_folder[ws_hash]

        # Generate a more descriptive title
        project_name = os.path.basename(cwd) if cwd else ws_hash[:8]
        title = f"VS Code Panel: {project_name} ({len(tool_outputs)} tool calls)"

        results.append({
            "session": {
                "id": session_id,
                "source": "vscode-panel",
                "server_name": server_name,
                "title": title,
                "cwd": cwd or None,
                "model": None,
                "cli_version": None,
                "start_time": start_time,
                "end_time": end_time,
                "message_count": len(messages),
                "metadata": {
                    "workspace_hash": ws_hash,
                    "session_uuid": session_uuid,
                    "tool_call_count": len(tool_outputs),
                },
                "raw_path": entries[0][1],
            },
            "messages": messages,
        })

    return results


def parse_vscode_debug_logs(vscode_dir: str, server_name: str = "local",
                            known_session_ids: set = None) -> list:
    """Parse VS Code Copilot Chat debug-log JSONL files.

    When github.copilot.chat.agentDebugLog.fileLogging.enabled is true,
    VS Code writes comprehensive conversation data (user messages, AI responses,
    tool calls, LLM requests) to:
      workspaceStorage/<hash>/GitHub.copilot-chat/debug-logs/<session-uuid>/main.jsonl

    Each line is a JSON object with:
      ts, dur, sid, type, name, spanId, status, attrs
    Types include: session_start, user_message, agent_response, tool_call,
                   llm_request, turn_start, turn_end, subagent, etc.

    Returns list of session-like dicts with source='vscode-debug-log'.
    """
    pattern = os.path.join(
        vscode_dir,
        "data/User/workspaceStorage/*/GitHub.copilot-chat/debug-logs",
    )
    debug_log_dirs = sorted(glob_mod.glob(pattern))
    if not debug_log_dirs:
        return []

    if known_session_ids is None:
        known_session_ids = set()

    uuid_re = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    results = []

    for dl_dir in debug_log_dirs:
        # Extract workspace hash
        parts = dl_dir.replace("\\", "/").split("/")
        ws_hash = ""
        for i, p in enumerate(parts):
            if p == "workspaceStorage" and i + 1 < len(parts):
                ws_hash = parts[i + 1]
                break

        try:
            session_dirs = os.listdir(dl_dir)
        except OSError:
            continue

        for session_uuid in session_dirs:
            if not uuid_re.match(session_uuid):
                continue

            session_dir = os.path.join(dl_dir, session_uuid)
            if not os.path.isdir(session_dir):
                continue

            # Find JSONL files (main.jsonl or child session files)
            jsonl_files = []
            try:
                for fname in os.listdir(session_dir):
                    if fname.endswith(".jsonl"):
                        jsonl_files.append(os.path.join(session_dir, fname))
            except OSError:
                continue

            if not jsonl_files:
                continue

            # Check if already tracked
            debug_log_id = f"vscode-debug-log-{server_name}-{session_uuid}"
            if debug_log_id in known_session_ids:
                continue

            panel_id = f"vscode-panel-{server_name}-{session_uuid}"

            # Parse all JSONL entries
            entries = []
            for jf in sorted(jsonl_files):
                try:
                    with open(jf, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entries.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                except OSError:
                    continue

            if not entries:
                continue

            # Convert entries to messages
            messages = []
            title = ""
            cwd = ""
            model = ""
            first_user_msg = ""

            for entry in entries:
                entry_type = entry.get("type", "")
                attrs = entry.get("attrs", {})
                ts_ms = entry.get("ts", 0)
                if ts_ms:
                    ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
                else:
                    ts = None

                if entry_type == "user_message":
                    content = attrs.get("content", "")
                    if content:
                        if not first_user_msg:
                            first_user_msg = content[:200]
                        messages.append({
                            "role": "user",
                            "content": content,
                            "msg_type": "text",
                            "timestamp": ts,
                        })

                elif entry_type == "agent_response":
                    response = attrs.get("response", "")
                    reasoning = attrs.get("reasoning", "")
                    content_parts = []
                    if reasoning:
                        content_parts.append(f"<thinking>\n{reasoning}\n</thinking>")
                    if response:
                        content_parts.append(response)
                    if content_parts:
                        messages.append({
                            "role": "assistant",
                            "content": "\n\n".join(content_parts),
                            "msg_type": "text",
                            "timestamp": ts,
                        })

                elif entry_type == "tool_call":
                    tool_name = entry.get("name", "unknown_tool")
                    args = attrs.get("args", "")
                    result = attrs.get("result", "")
                    error = attrs.get("error", "")
                    dur_ms = entry.get("dur", 0)

                    content_parts = [f"**Tool: {tool_name}**"]
                    if args:
                        args_display = args[:2000] + "..." if len(args) > 2000 else args
                        content_parts.append(f"Arguments: {args_display}")
                    if result:
                        result_display = result[:5000] + "..." if len(result) > 5000 else result
                        content_parts.append(f"Result: {result_display}")
                    if error:
                        content_parts.append(f"Error: {error}")
                    if dur_ms:
                        content_parts.append(f"Duration: {dur_ms}ms")

                    messages.append({
                        "role": "tool",
                        "content": "\n".join(content_parts),
                        "msg_type": "tool_call",
                        "timestamp": ts,
                        "metadata": {"tool_name": tool_name},
                    })

                elif entry_type == "llm_request":
                    req_model = attrs.get("model", "")
                    if req_model and not model:
                        model = req_model

                elif entry_type == "subagent":
                    agent_name = entry.get("name", attrs.get("agentName", "subagent"))
                    desc = attrs.get("description", "")
                    dur_ms = entry.get("dur", 0)
                    content = f"**Subagent: {agent_name}**"
                    if desc:
                        content += f"\n{desc}"
                    if dur_ms:
                        content += f"\nDuration: {dur_ms}ms"
                    messages.append({
                        "role": "assistant",
                        "content": content,
                        "msg_type": "subagent",
                        "timestamp": ts,
                    })

            if not messages:
                continue

            # Detect workspace from tool call content
            if not cwd:
                for msg in messages:
                    if msg["role"] in ("tool", "user"):
                        cwd = _detect_workspace_from_content(msg.get("content", ""))
                        if cwd:
                            break
            if not cwd:
                cwd = _detect_workspace_from_copilot_dir(ws_hash, vscode_dir)

            # Build timestamps from messages
            msg_timestamps = [m["timestamp"] for m in messages if m.get("timestamp")]
            start_time = min(msg_timestamps) if msg_timestamps else None
            end_time = max(msg_timestamps) if msg_timestamps else None

            # Build title
            if first_user_msg:
                title = first_user_msg[:100]
                if len(first_user_msg) > 100:
                    title += "..."
            else:
                project_name = os.path.basename(cwd) if cwd else ws_hash[:8]
                title = f"VS Code Chat: {project_name}"

            # Count message types
            user_count = sum(1 for m in messages if m["role"] == "user")
            assistant_count = sum(1 for m in messages if m["role"] == "assistant")
            tool_count = sum(1 for m in messages if m["role"] == "tool")

            results.append({
                "session": {
                    "id": debug_log_id,
                    "source": "vscode-debug-log",
                    "server_name": server_name,
                    "title": title,
                    "cwd": cwd or None,
                    "model": model or None,
                    "cli_version": None,
                    "start_time": start_time,
                    "end_time": end_time,
                    "message_count": len(messages),
                    "metadata": {
                        "workspace_hash": ws_hash,
                        "session_uuid": session_uuid,
                        "user_messages": user_count,
                        "assistant_messages": assistant_count,
                        "tool_calls": tool_count,
                        "replaces_panel_id": panel_id,
                    },
                    "raw_path": session_dir,
                },
                "messages": messages,
            })

    return results
