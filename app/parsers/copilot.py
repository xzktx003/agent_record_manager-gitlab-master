"""
Parser for GitHub Copilot CLI session data.
Format: ~/.copilot/session-state/<uuid>/events.jsonl
- Extracts ALL tool arguments (complete JSON, not selective keys)
- Stores full tool output (up to 50KB per message)
- Title from first 200 chars of first user message
- Reads vscode.metadata.json for workspace info
- Also handles Copilot Coding Agent sessions (workspace.yaml format)
"""
import json
import os
from typing import Optional

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

MAX_CONTENT_LEN = 500000  # 500KB per message — avoid truncation


def _read_vscode_metadata(session_dir: str) -> dict:
    """Read vscode.metadata.json from session dir for workspace and firstUserMessage."""
    meta_file = os.path.join(session_dir, "vscode.metadata.json")
    if not os.path.exists(meta_file):
        return {}
    try:
        with open(meta_file, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        result = {}
        wf = data.get("workspaceFolder", "")
        if isinstance(wf, dict):
            wf = wf.get("folderPath", "") or wf.get("folder_path", "")
        if wf:
            result["workspace_folder"] = wf
        fum = data.get("firstUserMessage", "")
        if fum:
            result["first_user_message"] = fum
        result["written_to_disc"] = data.get("writtenToDisc", False)
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def parse_copilot_session(session_dir: str, server_name: str = "local") -> Optional[dict]:
    events_file = os.path.join(session_dir, "events.jsonl")
    if not os.path.exists(events_file):
        return None

    session_id = os.path.basename(session_dir)
    # Read vscode metadata for workspace info
    vscode_meta = _read_vscode_metadata(session_dir)

    session_data = {
        "id": f"copilot-{server_name}-{session_id}",
        "source": "copilot",
        "server_name": server_name,
        "raw_path": session_dir,
    }
    messages = []
    start_time = None
    end_time = None

    try:
        with open(events_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")
                data = event.get("data", {})
                ts = event.get("timestamp")

                if etype == "session.start":
                    session_data["cwd"] = data.get("context", {}).get("cwd")
                    session_data["cli_version"] = data.get("copilotVersion")
                    session_data["start_time"] = data.get("startTime") or ts
                    start_time = session_data["start_time"]
                    session_data["metadata"] = {
                        "producer": data.get("producer"),
                        "version": data.get("version"),
                    }

                elif etype == "user.message":
                    content = data.get("content", "")
                    if content:
                        messages.append({
                            "role": "user",
                            "content": content[:MAX_CONTENT_LEN],
                            "msg_type": etype,
                            "timestamp": ts,
                        })
                        if not session_data.get("title") and content:
                            session_data["title"] = content[:200].replace("\n", " ")

                elif etype == "assistant.message":
                    content = data.get("content", "")
                    tool_requests = data.get("toolRequests", [])
                    if content:
                        messages.append({
                            "role": "assistant",
                            "content": content[:MAX_CONTENT_LEN],
                            "msg_type": etype,
                            "timestamp": ts,
                        })
                    # Extract ALL tool request arguments — full JSON serialization
                    if tool_requests:
                        for tool in tool_requests:
                            tool_name = tool.get("name", "unknown")
                            tool_args = tool.get("arguments", {})
                            if isinstance(tool_args, dict):
                                args_str = json.dumps(tool_args, ensure_ascii=False)
                            else:
                                args_str = str(tool_args)
                            if len(args_str) > MAX_CONTENT_LEN:
                                args_str = args_str[:MAX_CONTENT_LEN] + "...[truncated]"
                            messages.append({
                                "role": "assistant",
                                "content": f"[Tool: {tool_name}] {args_str}",
                                "msg_type": "tool_call",
                                "timestamp": ts,
                                "metadata": {"tool_name": tool_name},
                            })

                elif etype == "tool.execution_complete":
                    result = data.get("result", "")
                    tool_id = data.get("toolCallId", "")
                    if result:
                        result_str = str(result)
                        if len(result_str) > MAX_CONTENT_LEN:
                            result_str = result_str[:MAX_CONTENT_LEN] + "...[truncated]"
                        messages.append({
                            "role": "tool",
                            "content": result_str,
                            "msg_type": etype,
                            "timestamp": ts,
                            "metadata": {"toolCallId": tool_id} if tool_id else None,
                        })

                elif etype == "selection":
                    sel_data = data or {}
                    text = sel_data.get("text", "")
                    if text:
                        if len(text) > 10000:
                            text = text[:10000] + "...[truncated]"
                        messages.append({
                            "role": "system",
                            "content": f"[Selection from {sel_data.get('filePath', 'unknown')}]\n{text}",
                            "msg_type": etype,
                            "timestamp": ts,
                        })

                if ts:
                    end_time = ts

    except Exception as e:
        session_data["metadata"] = session_data.get("metadata", {})
        session_data["metadata"]["parse_error"] = str(e)

    # Enrich from vscode.metadata.json
    if vscode_meta.get("workspace_folder"):
        if not session_data.get("cwd"):
            session_data["cwd"] = vscode_meta["workspace_folder"]
        meta = session_data.get("metadata") or {}
        meta["workspace_folder"] = vscode_meta["workspace_folder"]
        session_data["metadata"] = meta
    if vscode_meta.get("first_user_message") and not session_data.get("title"):
        session_data["title"] = vscode_meta["first_user_message"][:200].replace("\n", " ")

    session_data["end_time"] = end_time
    session_data["message_count"] = len(messages)
    return {"session": session_data, "messages": messages}


def _read_yaml(path: str) -> dict:
    """Read a YAML file, falling back to basic parsing if PyYAML is not installed."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if _yaml:
            return _yaml.safe_load(text) or {}
        # Minimal key: value parser fallback
        result = {}
        for line in text.splitlines():
            if ":" in line and not line.startswith("#"):
                key, _, val = line.partition(":")
                result[key.strip()] = val.strip()
        return result
    except Exception:
        return {}


def parse_copilot_coding_agent(session_dir: str, server_name: str = "local") -> Optional[dict]:
    """Parse a Copilot Coding Agent session (workspace.yaml format, no events.jsonl).

    These sessions are created by GitHub Copilot Coding Agent and contain
    workspace.yaml + checkpoints/ + files/ + research/ directories.
    """
    ws_file = os.path.join(session_dir, "workspace.yaml")
    if not os.path.exists(ws_file):
        return None

    session_id = os.path.basename(session_dir)
    ws_data = _read_yaml(ws_file)

    session_data = {
        "id": f"copilot-agent-{server_name}-{session_id}",
        "source": "copilot",
        "server_name": server_name,
        "raw_path": session_dir,
        "cwd": ws_data.get("cwd", ""),
        "start_time": ws_data.get("created_at"),
        "end_time": ws_data.get("updated_at"),
        "metadata": {
            "type": "coding_agent",
            "git_root": ws_data.get("git_root", ""),
            "branch": ws_data.get("branch", ""),
            "summary_count": ws_data.get("summary_count", 0),
        },
    }

    # Build title from workspace info
    cwd = ws_data.get("cwd", "")
    branch = ws_data.get("branch", "")
    project = os.path.basename(cwd) if cwd else session_id
    session_data["title"] = f"[Coding Agent] {project}" + (f" ({branch})" if branch else "")

    # Check checkpoints for any content
    messages = []
    checkpoints_dir = os.path.join(session_dir, "checkpoints")
    if os.path.isdir(checkpoints_dir):
        for fname in sorted(os.listdir(checkpoints_dir)):
            fpath = os.path.join(checkpoints_dir, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    if content.strip() and "| # | Title | File |" not in content:
                        # Non-empty checkpoint with actual content
                        messages.append({
                            "role": "system",
                            "content": f"[Checkpoint: {fname}]\n{content[:MAX_CONTENT_LEN]}",
                            "msg_type": "checkpoint",
                            "timestamp": ws_data.get("created_at"),
                        })
                except Exception:
                    pass

    session_data["message_count"] = len(messages)
    return {"session": session_data, "messages": messages}
