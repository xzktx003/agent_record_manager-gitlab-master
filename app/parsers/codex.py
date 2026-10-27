"""
Parser for OpenAI Codex CLI session data.
- Full tool arguments (no truncation except at 50KB)
- Full tool output (no more 2000/5000 char limits)
- Better title extraction from session_index
"""
import json
import os
import glob as glob_mod
import re
from datetime import datetime, timedelta
from typing import Optional, List

MAX_CONTENT_LEN = 500000  # 500KB per message — avoid truncation
_TEAM_WORKER_PATTERN = re.compile(r"/worker-(\d+)/inbox\.md", re.IGNORECASE)
_TEAM_SUMMARIZER_PREFIX = "You summarize shell command output."
_INTERNAL_INPUT_PREFIXES = (
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "# AGENTS.md instructions",
    "<environment_context>",
    "<hook_prompt",
    "<skill>",
)
_OMX_MARKERS = (
    "oh-my-codex",
    "You are running with oh-my-codex",
    "omx:generated",
    "OMX:RUNTIME:",
    "OMX:TEAM:",
)


def _normalize_content_parts(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for item in content:
        if isinstance(item, dict):
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item.get("content"), str):
                parts.append(item["content"])
            elif item.get("type"):
                parts.append(str(item["type"]))
        elif item is not None:
            parts.append(str(item))
    return "\n".join(part for part in parts if part)


def _is_internal_input_text(text: str) -> bool:
    if not text:
        return False
    stripped = text.lstrip()
    if stripped.startswith(_INTERNAL_INPUT_PREFIXES):
        return True
    return "hook_run_id=\"stop:" in stripped


def _normalize_tool_output(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _detect_omx_metadata(payload: dict) -> dict:
    base_instructions = payload.get("base_instructions")
    if isinstance(base_instructions, dict):
        text = base_instructions.get("text", "")
    elif isinstance(base_instructions, str):
        text = base_instructions
    else:
        text = ""

    markers = [marker for marker in _OMX_MARKERS if marker in text]
    return {
        "managed": bool(markers),
        "markers": markers[:3],
    }


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _sort_messages(messages: List[dict]) -> List[dict]:
    return [
        msg for _, msg in sorted(
            enumerate(messages),
            key=lambda item: ((item[1].get("timestamp") or ""), item[0]),
        )
    ]


def _first_user_content(result: dict) -> str:
    for msg in result.get("messages", []):
        if msg.get("role") == "user" and msg.get("content"):
            return msg["content"]
    return ""


def _normalize_team_root(cwd: Optional[str]) -> str:
    if not cwd:
        return ""
    normalized = cwd.replace("\\", "/").rstrip("/")
    normalized = re.sub(r"/\.omx/team/.*$", "", normalized)
    parent, name = os.path.split(normalized)
    trimmed = re.sub(r"__team[^/]*$", "", name)
    if trimmed and trimmed != name:
        normalized = os.path.join(parent, trimmed).replace("\\", "/")
    return normalized.rstrip("/")


def _team_session_kind(result: dict, meta: dict) -> Optional[str]:
    title = result["session"].get("title") or ""
    first_user = _first_user_content(result)
    title_lower = title.lower()
    first_lower = first_user.lower()

    if _TEAM_WORKER_PATTERN.search(title) or _TEAM_WORKER_PATTERN.search(first_user):
        return "worker"
    if (
        title.startswith(_TEAM_SUMMARIZER_PREFIX)
        or first_user.startswith(_TEAM_SUMMARIZER_PREFIX)
    ) and meta.get("source") == "exec":
        return "helper"
    if "$team" in title_lower or "$team" in first_lower:
        return "controller"
    omx_meta = meta.get("omx") if isinstance(meta.get("omx"), dict) else {}
    if (
        omx_meta.get("managed")
        and meta.get("originator") == "codex-tui"
        and meta.get("source") == "cli"
    ):
        return "controller"
    return None


def _team_child_label(result: dict, kind: str) -> str:
    cwd = result["session"].get("cwd") or ""
    title = result["session"].get("title") or ""
    worker_match = _TEAM_WORKER_PATTERN.search(title) or _TEAM_WORKER_PATTERN.search(_first_user_content(result))
    if kind == "worker" and worker_match:
        return f"[team-worker-{worker_match.group(1)}]"
    if kind == "helper":
        return "[team-helper]"
    base = os.path.basename(cwd.rstrip("/")) if cwd else kind
    return f"[{kind}:{base}]"


def _attach_child_session(parent: dict, child: dict, label: str, merge_kind: str):
    parent_meta = parent["session"].setdefault("metadata", {}) or {}
    if isinstance(parent_meta, str):
        try:
            parent_meta = json.loads(parent_meta)
        except Exception:
            parent_meta = {}
        parent["session"]["metadata"] = parent_meta
    parent_meta.setdefault("merged_child_sessions", []).append({
        "session_id": child["session"].get("id"),
        "label": label,
        "merge_kind": merge_kind,
    })

    parent["messages"].append({
        "role": "system",
        "content": f"--- Merged {label} ---",
        "msg_type": f"{merge_kind}_start",
        "timestamp": child["session"].get("start_time"),
        "metadata": {"subagent": label, "merge_kind": merge_kind},
    })

    for msg in child.get("messages", []):
        merged_msg = dict(msg)
        msg_meta = merged_msg.get("metadata") or {}
        if isinstance(msg_meta, str):
            try:
                msg_meta = json.loads(msg_meta)
            except Exception:
                msg_meta = {}
        msg_meta["subagent"] = label
        msg_meta["merge_kind"] = merge_kind
        msg_meta["merged_from_session_id"] = child["session"].get("id")
        merged_msg["metadata"] = msg_meta
        if merged_msg.get("role") == "assistant":
            merged_msg["content"] = f"{label} {merged_msg.get('content', '')}"
        parent["messages"].append(merged_msg)

    parent.setdefault("_source_files", []).extend(child.get("_source_files", []))


def _find_team_parent(child: dict, controllers: List[tuple[dict, dict]]) -> Optional[dict]:
    child_root = _normalize_team_root(child["session"].get("cwd"))
    if not child_root:
        return None

    child_start = _parse_timestamp(child["session"].get("start_time"))
    child_end = _parse_timestamp(child["session"].get("end_time"))
    best_parent = None
    best_score = None

    for controller, meta in controllers:
        if _normalize_team_root(controller["session"].get("cwd")) != child_root:
            continue

        controller_start = _parse_timestamp(controller["session"].get("start_time"))
        controller_end = _parse_timestamp(controller["session"].get("end_time"))
        if child_start and controller_start:
            if child_start < controller_start - timedelta(minutes=10):
                continue
            if child_start > controller_start + timedelta(hours=3):
                continue

        score = controller["session"].get("message_count", 0)
        if child_start and controller_start:
            gap = abs((child_start - controller_start).total_seconds())
            score -= gap / 60.0
        if child_start and controller_end and child_start <= controller_end + timedelta(minutes=30):
            score += 200
        if "$team" in (controller["session"].get("title") or "").lower():
            score += 100
        if meta.get("originator") == "codex-tui":
            score += 20
        if child_end and controller_end and child_end <= controller_end + timedelta(hours=1):
            score += 20

        if best_score is None or score > best_score:
            best_parent = controller
            best_score = score

    return best_parent


def _merge_team_sessions_without_parent(root_results: List[dict], logger) -> List[dict]:
    controllers: List[tuple[dict, dict]] = []
    candidates: List[tuple[dict, dict, str]] = []

    for result in root_results:
        meta = result["session"].get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        kind = _team_session_kind(result, meta)
        if kind == "controller":
            controllers.append((result, meta))
        elif kind in {"worker", "helper"}:
            candidates.append((result, meta, kind))

    if not controllers or not candidates:
        return root_results

    merged_ids = set()
    merged_count = 0
    for child, _meta, kind in candidates:
        parent = _find_team_parent(child, controllers)
        if not parent or parent["session"].get("id") == child["session"].get("id"):
            continue
        label = _team_child_label(child, kind)
        _attach_child_session(parent, child, label, "team_merge")
        merged_ids.add(child["session"].get("id"))
        merged_count += 1

    if merged_count:
        logger.info(f"Heuristically merged {merged_count} team sessions without parent metadata")

    return [
        result for result in root_results
        if result["session"].get("id") not in merged_ids
    ]


def _append_assistant_message(messages: List[dict], content: str, msg_type: str,
                              timestamp: Optional[str], recent_assistant_texts: List[str],
                              seen_assistant_texts: set[str]) -> bool:
    if not content:
        return False
    if len(content) > 200 and content in seen_assistant_texts:
        return False
    if content in recent_assistant_texts[-3:]:
        return False
    messages.append({
        "role": "assistant",
        "content": content[:MAX_CONTENT_LEN],
        "msg_type": msg_type,
        "timestamp": timestamp,
    })
    recent_assistant_texts.append(content)
    seen_assistant_texts.add(content)
    if len(recent_assistant_texts) > 8:
        del recent_assistant_texts[:-8]
    return True


def _append_user_message(messages: List[dict], content: str, msg_type: str,
                         timestamp: Optional[str]) -> bool:
    if not content:
        return False
    current_ts = _parse_timestamp(timestamp)
    for recent in reversed(messages[-4:]):
        if recent.get("role") != "user":
            continue
        if recent.get("content") != content:
            continue
        recent_ts = _parse_timestamp(recent.get("timestamp"))
        if current_ts and recent_ts:
            if abs((current_ts - recent_ts).total_seconds()) <= 5:
                return False
        else:
            return False

    messages.append({
        "role": "user",
        "content": content[:MAX_CONTENT_LEN],
        "msg_type": msg_type,
        "timestamp": timestamp,
    })
    return True

def parse_codex_index(codex_dir: str) -> List[dict]:
    """Parse session_index.jsonl to get session list with titles."""
    index_file = os.path.join(codex_dir, "session_index.jsonl")
    if not os.path.exists(index_file):
        return []
    sessions = []
    with open(index_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                sessions.append(entry)
            except json.JSONDecodeError:
                continue
    return sessions

def parse_codex_session(rollout_file: str, server_name: str = "local",
                       index_entry: dict = None) -> Optional[dict]:
    if not os.path.exists(rollout_file):
        return None

    filename = os.path.basename(rollout_file)
    parts = filename.replace(".jsonl", "").split("-")
    if len(parts) >= 5:
        uuid_part = "-".join(parts[-5:])
    else:
        uuid_part = filename

    session_data = {
        "id": f"codex-{server_name}-{uuid_part}",
        "source": "codex",
        "server_name": server_name,
        "raw_path": rollout_file,
    }
    messages = []
    end_time = None
    recent_assistant_texts: List[str] = []
    seen_assistant_texts: set[str] = set()

    try:
        with open(rollout_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")
                payload = event.get("payload", {})
                ts = event.get("timestamp")

                if etype == "session_meta":
                    session_data["cwd"] = payload.get("cwd")
                    session_data["cli_version"] = payload.get("cli_version")
                    session_data["model"] = payload.get("model_provider")
                    session_data["start_time"] = payload.get("timestamp") or ts
                    # Detect subagent sessions
                    source_info = payload.get("source", {})
                    subagent_info = None
                    if isinstance(source_info, dict) and "subagent" in source_info:
                        spawn = source_info["subagent"].get("thread_spawn", {})
                        subagent_info = {
                            "parent_thread_id": spawn.get("parent_thread_id"),
                            "agent_role": spawn.get("agent_role"),
                            "agent_nickname": spawn.get("agent_nickname"),
                            "depth": spawn.get("depth", 1),
                        }
                    session_data["metadata"] = {
                        "originator": payload.get("originator"),
                        "source": payload.get("source"),
                        "codex_id": payload.get("id"),
                        "subagent": subagent_info,
                        "omx": _detect_omx_metadata(payload),
                    }

                elif etype == "event_msg":
                    inner_type = payload.get("type", "")
                    if inner_type == "user_message":
                        content = _normalize_content_parts(payload.get("message", ""))
                        if _append_user_message(messages, content, inner_type, ts):
                            if not session_data.get("title") and content:
                                lines = content.split("\n")
                            for text_line in lines:
                                if (
                                    text_line.strip()
                                    and not text_line.startswith("#")
                                    and not text_line.startswith("-")
                                ):
                                    session_data["title"] = text_line.strip()[:200]
                                    break
                                if not session_data.get("title"):
                                    session_data["title"] = content[:200]

                    elif inner_type == "agent_message":
                        content = _normalize_content_parts(payload.get("message", ""))
                        _append_assistant_message(
                            messages,
                            content,
                            "agent_message",
                            ts,
                            recent_assistant_texts,
                            seen_assistant_texts,
                        )

                    elif inner_type == "exec_command_end":
                        cmd = payload.get("command", "")
                        if isinstance(cmd, list):
                            cmd = " ".join(cmd)
                        output = payload.get("aggregated_output") or payload.get("stdout") or ""
                        stderr = payload.get("stderr", "")
                        exit_code = payload.get("exit_code", "")
                        cwd = payload.get("cwd", "")
                        parts_list = []
                        if cmd:
                            parts_list.append(f"$ {cmd}")
                        if cwd:
                            parts_list.append(f"[cwd: {cwd}]")
                        if output:
                            parts_list.append(str(output))
                        if stderr:
                            parts_list.append(f"[stderr] {stderr}")
                        if exit_code not in ("", 0, None):
                            parts_list.append(f"[exit_code: {exit_code}]")
                        content = "\n".join(parts_list)
                        if content:
                            messages.append({
                                "role": "tool",
                                "content": content[:MAX_CONTENT_LEN],
                                "msg_type": "exec_command",
                                "timestamp": ts,
                                "metadata": {"tool_name": "shell", "exit_code": exit_code},
                            })

                    elif inner_type == "mcp_tool_call_end":
                        inv = payload.get("invocation", {})
                        tool_name = inv.get("tool", "mcp_tool")
                        server = inv.get("server", "")
                        args = inv.get("arguments", {})
                        result = payload.get("result", {})
                        result_text = ""
                        ok = result.get("Ok", {})
                        if ok:
                            contents_list = ok.get("content", [])
                            for c in contents_list:
                                if c.get("type") == "text":
                                    result_text += c.get("text", "")
                        err = result.get("Err")
                        if err:
                            result_text = f"[Error] {err}"
                        parts_list = [f"[MCP: {server}/{tool_name}]"]
                        if args:
                            args_str = json.dumps(args, ensure_ascii=False)
                            if len(args_str) > 500:
                                args_str = args_str[:500] + "..."
                            parts_list.append(f"Args: {args_str}")
                        if result_text:
                            parts_list.append(result_text)
                        content = "\n".join(parts_list)
                        messages.append({
                            "role": "tool",
                            "content": content[:MAX_CONTENT_LEN],
                            "msg_type": "mcp_tool_call",
                            "timestamp": ts,
                            "metadata": {"tool_name": tool_name, "server": server},
                        })

                    elif inner_type == "patch_apply_end":
                        changes = payload.get("changes", [])
                        status = payload.get("status", "")
                        stdout = payload.get("stdout", "")
                        parts_list = [f"[Patch Apply] status={status}"]
                        if stdout:
                            parts_list.append(stdout)
                        for ch in (changes or []):
                            parts_list.append(str(ch))
                        content = "\n".join(parts_list)
                        messages.append({
                            "role": "tool",
                            "content": content[:MAX_CONTENT_LEN],
                            "msg_type": "patch_apply",
                            "timestamp": ts,
                            "metadata": {"tool_name": "patch"},
                        })

                    elif inner_type == "web_search_end":
                        query = payload.get("query", "")
                        action = payload.get("action", "")
                        content = f"[Web Search] {query}"
                        if action:
                            content += f"\n{json.dumps(action, ensure_ascii=False)}" if isinstance(action, dict) else f"\n{action}"
                        messages.append({
                            "role": "tool",
                            "content": content[:MAX_CONTENT_LEN],
                            "msg_type": "web_search",
                            "timestamp": ts,
                            "metadata": {"tool_name": "web_search"},
                        })

                elif etype == "response_item":
                    payload_type = payload.get("type", "")
                    role = payload.get("role", "assistant")

                    if payload_type == "message":
                        content = _normalize_content_parts(payload.get("content") or [])
                        if role == "assistant":
                            _append_assistant_message(
                                messages,
                                content,
                                "agent_message",
                                ts,
                                recent_assistant_texts,
                                seen_assistant_texts,
                            )
                        elif role == "user":
                            if content and not _is_internal_input_text(content):
                                _append_user_message(messages, content, "input_text", ts)
                        elif role in {"developer", "system"}:
                            continue

                    elif payload_type == "function_call":
                        fn_name = payload.get("name", "unknown")
                        args_str = _normalize_tool_output(payload.get("arguments", ""))
                        if len(args_str) > MAX_CONTENT_LEN:
                            args_str = args_str[:MAX_CONTENT_LEN] + "...[truncated]"
                        messages.append({
                            "role": "assistant",
                            "content": f"[Tool: {fn_name}] {args_str}",
                            "msg_type": "function_call",
                            "timestamp": ts,
                            "metadata": {
                                "tool_name": fn_name,
                                "call_id": payload.get("call_id"),
                            },
                        })

                    elif payload_type == "function_call_output":
                        output = _normalize_tool_output(payload.get("output"))
                        if output:
                            messages.append({
                                "role": "tool",
                                "content": output[:MAX_CONTENT_LEN],
                                "msg_type": "function_call_output",
                                "timestamp": ts,
                                "metadata": {"call_id": payload.get("call_id")},
                            })

                    elif payload_type == "reasoning":
                        summary = payload.get("summary")
                        content = _normalize_content_parts(summary if summary else payload.get("content"))
                        if content:
                            messages.append({
                                "role": "assistant",
                                "content": f"[Reasoning] {content[:MAX_CONTENT_LEN]}",
                                "msg_type": "reasoning",
                                "timestamp": ts,
                            })

                elif etype == "function_call":
                    fn_name = payload.get("name", event.get("name", "unknown"))
                    fn_args = payload.get("arguments", "")
                    # Full serialization of function args
                    if isinstance(fn_args, dict):
                        args_str = json.dumps(fn_args, ensure_ascii=False)
                    else:
                        args_str = str(fn_args)
                    if len(args_str) > MAX_CONTENT_LEN:
                        args_str = args_str[:MAX_CONTENT_LEN] + "...[truncated]"
                    messages.append({
                        "role": "assistant",
                        "content": f"[Tool: {fn_name}] {args_str}",
                        "msg_type": "function_call",
                        "timestamp": ts,
                        "metadata": {"tool_name": fn_name},
                    })

                elif etype == "function_call_output":
                    output = payload.get("output", event.get("output", ""))
                    if output:
                        output_str = str(output)
                        if len(output_str) > MAX_CONTENT_LEN:
                            output_str = output_str[:MAX_CONTENT_LEN] + "...[truncated]"
                        messages.append({
                            "role": "tool",
                            "content": output_str,
                            "msg_type": "function_call_output",
                            "timestamp": ts,
                        })

                elif etype == "reasoning":
                    text = payload.get("text", event.get("text", ""))
                    if text:
                        messages.append({
                            "role": "assistant",
                            "content": f"[Reasoning] {text}",
                            "msg_type": "reasoning",
                            "timestamp": ts,
                        })

                if ts:
                    end_time = ts

    except Exception as e:
        session_data["metadata"] = session_data.get("metadata", {})
        session_data["metadata"]["parse_error"] = str(e)

    # Override title from index if available
    if index_entry and index_entry.get("thread_name"):
        session_data["title"] = index_entry["thread_name"]

    session_data["end_time"] = end_time
    session_data["message_count"] = len(messages)
    return {"session": session_data, "messages": messages}

def find_codex_rollout_files(codex_dir: str) -> List[str]:
    sessions_dir = os.path.join(codex_dir, "sessions")
    if not os.path.exists(sessions_dir):
        return []
    return sorted(glob_mod.glob(
        os.path.join(sessions_dir, "**", "rollout-*.jsonl"),
        recursive=True
    ))


def scan_codex_rollout_file(rollout_file: str,
                            index_entry: dict = None) -> Optional[dict]:
    """Read only enough of a rollout to plan incremental sync groups.

    Full parsing every rollout kept hundreds of thousands of messages alive at
    once. The session header plus first user message is sufficient to group
    same-thread rollouts, explicit subagents, and OMX team workers before
    deciding which small group actually changed.
    """
    if not os.path.exists(rollout_file):
        return None
    filename = os.path.basename(rollout_file)
    parts = filename.replace(".jsonl", "").split("-")
    uuid_part = "-".join(parts[-5:]) if len(parts) >= 5 else filename
    descriptor = {
        "path": rollout_file,
        "uuid": uuid_part,
        "codex_id": uuid_part,
        "parent_thread_id": None,
        "cwd": "",
        "start_time": None,
        "title": (index_entry or {}).get("thread_name", ""),
        "first_user": "",
        "metadata": {},
        "team_kind": None,
    }
    try:
        file_stat = os.stat(rollout_file)
        descriptor["mtime"] = int(file_stat.st_mtime or 0)
        descriptor["size"] = int(file_stat.st_size or 0)
        with open(rollout_file, "r", encoding="utf-8", errors="replace") as file_obj:
            for line_no, line in enumerate(file_obj):
                if line_no > 200 and descriptor["metadata"]:
                    break
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                event_type = event.get("type", "")
                payload = event.get("payload", {})
                if event_type == "session_meta":
                    source_info = payload.get("source", {})
                    subagent_info = None
                    if isinstance(source_info, dict) and "subagent" in source_info:
                        spawn = source_info["subagent"].get("thread_spawn", {})
                        subagent_info = {
                            "parent_thread_id": spawn.get("parent_thread_id"),
                            "agent_role": spawn.get("agent_role"),
                            "agent_nickname": spawn.get("agent_nickname"),
                            "depth": spawn.get("depth", 1),
                        }
                    descriptor["codex_id"] = (
                        payload.get("id") or payload.get("session_id") or uuid_part
                    )
                    descriptor["parent_thread_id"] = (
                        (subagent_info or {}).get("parent_thread_id")
                    )
                    descriptor["cwd"] = payload.get("cwd") or ""
                    descriptor["start_time"] = (
                        payload.get("timestamp") or event.get("timestamp")
                    )
                    descriptor["metadata"] = {
                        "originator": payload.get("originator"),
                        "source": payload.get("source"),
                        "codex_id": descriptor["codex_id"],
                        "subagent": subagent_info,
                        "omx": _detect_omx_metadata(payload),
                    }
                elif event_type == "event_msg" and payload.get("type") == "user_message":
                    descriptor["first_user"] = _normalize_content_parts(
                        payload.get("message", "")
                    )
                elif event_type == "response_item":
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text = _normalize_content_parts(payload.get("content") or [])
                        if text and not _is_internal_input_text(text):
                            descriptor["first_user"] = text
                if descriptor["metadata"] and descriptor["first_user"]:
                    break
    except OSError:
        return None

    if not descriptor["title"] and descriptor["first_user"]:
        descriptor["title"] = descriptor["first_user"].splitlines()[0][:200]
    probe = {
        "session": {
            "id": descriptor["uuid"],
            "title": descriptor["title"],
            "cwd": descriptor["cwd"],
            "start_time": descriptor["start_time"],
            "message_count": max(1, descriptor["size"] // 1024),
            "metadata": descriptor["metadata"],
        },
        "messages": (
            [{"role": "user", "content": descriptor["first_user"]}]
            if descriptor["first_user"] else []
        ),
    }
    descriptor["team_kind"] = _team_session_kind(
        probe, descriptor["metadata"]
    )
    descriptor["team_root"] = _normalize_team_root(descriptor["cwd"])
    return descriptor


def merge_codex_subagents(parsed_results: List[dict]) -> List[dict]:
    """Merge same-session rollouts and subagent sessions into parent sessions.
    
    1. Multiple rollouts with the same codex_id are merged chronologically.
    2. Subagent rollouts are appended to their parent session with labels.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    def _get_meta(result):
        meta = result["session"].get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        return meta
    
    # Phase 1: Group by codex_id, merge same-session rollouts
    by_codex_id = {}  # codex_id -> merged parsed result
    no_codex_id = []
    subagents = []
    rollout_merged = 0
    
    for result in parsed_results:
        meta = _get_meta(result)
        subagent = meta.get("subagent")
        codex_id = meta.get("codex_id", "")
        
        if subagent and subagent.get("parent_thread_id"):
            subagents.append((subagent["parent_thread_id"], result))
        elif codex_id:
            if codex_id in by_codex_id:
                # Same session, additional rollout — merge messages chronologically
                existing = by_codex_id[codex_id]
                existing["messages"].extend(result["messages"])
                existing.setdefault("_source_files", []).extend(result.get("_source_files", []))
                # Update end_time if later
                new_end = result["session"].get("end_time")
                old_end = existing["session"].get("end_time")
                if new_end and (not old_end or new_end > old_end):
                    existing["session"]["end_time"] = new_end
                rollout_merged += 1
            else:
                by_codex_id[codex_id] = result
        else:
            no_codex_id.append(result)
    
    if rollout_merged > 0:
        logger.info(f"Merged {rollout_merged} additional rollouts into existing sessions")
    
    # Phase 2: Merge subagent sessions into their parents
    subagent_merged = 0
    orphan_subagents = []
    
    for parent_thread_id, sub_result in subagents:
        parent = by_codex_id.get(parent_thread_id)
        if not parent:
            orphan_subagents.append(sub_result)
            continue
        
        sub_meta = _get_meta(sub_result)
        sub_info = sub_meta.get("subagent", {})
        role_name = sub_info.get("agent_role", "subagent")
        nickname = sub_info.get("agent_nickname", "")
        label = f"[{role_name}:{nickname}]" if nickname else f"[{role_name}]"
        
        parent["messages"].append({
            "role": "system",
            "content": f"--- Subagent {label} ---",
            "msg_type": "subagent_start",
            "timestamp": sub_result["session"].get("start_time"),
        })
        
        for msg in sub_result["messages"]:
            merged_msg = dict(msg)
            if merged_msg["role"] == "assistant":
                merged_msg["content"] = f"{label} {merged_msg.get('content', '')}"
            merged_msg["metadata"] = merged_msg.get("metadata") or {}
            if isinstance(merged_msg["metadata"], str):
                try:
                    merged_msg["metadata"] = json.loads(merged_msg["metadata"])
                except Exception:
                    merged_msg["metadata"] = {}
            merged_msg["metadata"]["subagent"] = label
            parent["messages"].append(merged_msg)
        parent.setdefault("_source_files", []).extend(sub_result.get("_source_files", []))
        
        subagent_merged += 1
    
    final = list(by_codex_id.values()) + no_codex_id + orphan_subagents
    final = _merge_team_sessions_without_parent(final, logger)

    # Update message counts after every merge path and normalize chronology.
    for result in final:
        result["messages"] = _sort_messages(result["messages"])
        result["session"]["message_count"] = len(result["messages"])

    if subagent_merged > 0:
        logger.info(f"Merged {subagent_merged} subagent sessions into parent sessions")
    
    return final
