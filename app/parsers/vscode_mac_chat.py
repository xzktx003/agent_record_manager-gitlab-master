"""
Parser for VS Code Chat Session JSONL files exported from Mac desktop.

VS Code stores chat panel conversations in:
  ~/Library/Application Support/Code/User/workspaceStorage/<hash>/chatSessions/<uuid>.jsonl

JSONL format (incremental state):
  kind=0: Initial session state (full object with version, creationDate, sessionId, requests[], inputState, etc.)
  kind=1: Set value at key path (k=[path...], v=value)
  kind=2: Append to array at key path (k=[path...], v=[items...])

The session state is reconstructed by replaying these operations in order.

Requests array contains user messages and AI responses:
  request.message / request.text: user message
  request.response: array of response parts (markdown, toolInvocationSerialized, thinking, etc.)
  request.result: result metadata with renderedUserMessage, timings, etc.
"""
import json
import os
import re
import glob as glob_mod
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from urllib.parse import unquote

logger = logging.getLogger(__name__)


def parse_mac_chat_manifest(manifest_path: str) -> Dict:
    """Load the manifest mapping workspace hashes to workspace folders and session info."""
    if not os.path.exists(manifest_path):
        return {}
    with open(manifest_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _decode_workspace_folder(folder_uri: str) -> str:
    """Extract readable workspace path from VS Code workspace URI."""
    if not folder_uri:
        return ""
    # URL-decode first so %2B becomes +, etc.
    folder_uri = unquote(folder_uri)
    # Handle vscode-remote://ssh-remote+<host>/<path>
    m = re.match(r'vscode-remote://ssh-remote\+[^/]+(/.+)', folder_uri)
    if m:
        return m.group(1)
    # Handle file:///path
    if folder_uri.startswith('file:///'):
        return folder_uri[7:]
    if folder_uri.startswith('file://'):
        return folder_uri[7:]
    return folder_uri


def _replay_jsonl(filepath: str) -> Optional[Dict]:
    """Replay JSONL operations to reconstruct the full session state.

    Returns the reconstructed session state dict, or None on error.
    """
    state = None
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line_no, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                
                kind = entry.get('kind')
                
                if kind == 0:
                    # Initial state
                    state = entry.get('v', {})
                elif kind == 1 and state is not None:
                    # Set value at key path
                    keys = entry.get('k', [])
                    value = entry.get('v')
                    _set_at_path(state, keys, value)
                elif kind == 2 and state is not None:
                    # Append to array at key path
                    keys = entry.get('k', [])
                    items = entry.get('v', [])
                    _append_at_path(state, keys, items)
    except Exception as e:
        logger.error(f"Error replaying {filepath}: {e}")
        return None
    
    return state


def _set_at_path(obj: Any, keys: list, value: Any):
    """Set a value at a nested key path in the object."""
    if not keys:
        return
    current = obj
    for i, key in enumerate(keys[:-1]):
        if isinstance(current, dict):
            if key not in current:
                # Create intermediate container based on next key type
                next_key = keys[i + 1]
                current[key] = [] if isinstance(next_key, int) else {}
            current = current[key]
        elif isinstance(current, list):
            if isinstance(key, int):
                while len(current) <= key:
                    current.append({})
                current = current[key]
            else:
                return
        else:
            return
    
    last_key = keys[-1]
    if isinstance(current, dict):
        current[last_key] = value
    elif isinstance(current, list) and isinstance(last_key, int):
        while len(current) <= last_key:
            current.append(None)
        current[last_key] = value


def _append_at_path(obj: Any, keys: list, items: list):
    """Append items to an array at a nested key path."""
    if not keys:
        return
    current = obj
    for key in keys[:-1]:
        if isinstance(current, dict):
            if key not in current:
                current[key] = {} if not isinstance(key, int) else []
            current = current[key]
        elif isinstance(current, list) and isinstance(key, int):
            while len(current) <= key:
                current.append({})
            current = current[key]
        else:
            return
    
    last_key = keys[-1]
    target = None
    if isinstance(current, dict):
        if last_key not in current:
            current[last_key] = []
        target = current[last_key]
    elif isinstance(current, list) and isinstance(last_key, int):
        while len(current) <= last_key:
            current.append([])
        target = current[last_key]
    
    if isinstance(target, list) and isinstance(items, list):
        target.extend(items)


def _extract_text_from_response(response: list) -> str:
    """Extract readable text from a response parts array.

    Text fragments and inlineReferences are concatenated into continuous
    paragraphs. Block-level items (tools, thinking) get their own lines.
    """
    parts = []
    current_text = []  # accumulates inline fragments

    def _flush_text():
        joined = ''.join(current_text).strip()
        if joined:
            parts.append(joined)
        current_text.clear()

    for item in response:
        if not isinstance(item, dict):
            continue
        kind = item.get('kind', '')

        if kind == 'markdownContent':
            value = item.get('content', {})
            if isinstance(value, dict):
                text = value.get('value', '')
            elif isinstance(value, str):
                text = value
            else:
                text = str(value)
            if text:
                current_text.append(text)
        elif kind == 'inlineReference':
            ref = item.get('inlineReference', {})
            name = ref.get('name', '') if isinstance(ref, dict) else str(ref)
            if name:
                current_text.append(f"`{name}`")
        elif not kind and 'value' in item:
            text = item.get('value', '')
            if isinstance(text, str) and text.strip():
                current_text.append(text)
        elif kind == 'thinking':
            value = item.get('value', '')
            if isinstance(value, str) and value.strip():
                _flush_text()
                parts.append(f"[Thinking] {value}")
        elif kind == 'toolInvocationSerialized':
            _flush_text()
            past = item.get('pastTenseMessage', '')
            msg = item.get('invocationMessage', '')
            display = past if past else msg
            if isinstance(display, dict):
                display = display.get('value', '')
            if not display and isinstance(msg, dict):
                display = msg.get('value', '')
            result = item.get('result', '')
            if isinstance(result, dict):
                result = result.get('value', '')
                if isinstance(result, dict):
                    result = result.get('value', '')
            tool_text = f"[Tool] {display}"
            if result and isinstance(result, str) and len(result) < 1000:
                tool_text += f"\n{result}"
            parts.append(tool_text)
        elif kind == 'progressMessage':
            value = item.get('content', {})
            if isinstance(value, dict):
                text = value.get('value', '')
            elif isinstance(value, str):
                text = value
            else:
                text = ''
            if text:
                _flush_text()
                parts.append(f"[Progress] {text}")
        elif kind == 'codeblockUri':
            uri = item.get('uri', '')
            _flush_text()
            parts.append(f"[Code block: {uri}]")
        elif kind == 'warning':
            content = item.get('content', {})
            if isinstance(content, dict):
                text = content.get('value', '')
            elif isinstance(content, str):
                text = content
            else:
                text = ''
            if text:
                _flush_text()
                parts.append(f"[Warning] {text}")

    _flush_text()
    return '\n'.join(parts)


def _extract_user_message(request: dict) -> str:
    """Extract user message text from a request object."""
    # Try different possible locations for the user message
    msg = request.get('message', '')
    if isinstance(msg, dict):
        msg = msg.get('text', '') or msg.get('value', '')
    if msg:
        return msg
    
    # Try result.renderedUserMessage
    result = request.get('result', {})
    if isinstance(result, dict):
        rendered = result.get('metadata', {})
        if isinstance(rendered, dict):
            rendered_msg = rendered.get('renderedUserMessage', [])
            if isinstance(rendered_msg, list):
                texts = []
                for part in rendered_msg:
                    if isinstance(part, dict):
                        text = part.get('text', '')
                        if text:
                            texts.append(text)
                    elif isinstance(part, str):
                        texts.append(part)
                if texts:
                    return '\n'.join(texts)
    
    return ''


def parse_mac_chat_session(filepath: str, session_id: str = None,
                           workspace_folder: str = '', title: str = '',
                           server_name: str = 'mac') -> Optional[Dict]:
    """Parse a single VS Code Chat Session JSONL file from Mac.
    
    Args:
        filepath: Path to the .jsonl file
        session_id: Override session ID (defaults to filename without extension)
        workspace_folder: Workspace folder path for this session
        title: Session title from the index
        server_name: Server name tag

    Returns:
        Dict with 'session' and 'messages' keys, or None if parsing fails.
    """
    if not os.path.exists(filepath):
        return None
    
    # Replay JSONL to get full state
    state = _replay_jsonl(filepath)
    if not state:
        return None
    
    # Extract session info
    if not session_id:
        session_id = os.path.splitext(os.path.basename(filepath))[0]
    
    creation_date = state.get('creationDate', 0)
    session_uuid = state.get('sessionId', session_id)
    custom_title = state.get('customTitle', '')
    requests = state.get('requests', [])
    
    if not title:
        title = custom_title
    
    # If still no title, try to extract from first user message
    if not title and requests:
        first_msg = _extract_user_message(requests[0])
        if first_msg:
            title = first_msg[:100].split('\n')[0]
    
    if not title:
        title = f"Chat Session {session_uuid[:8]}"
    
    # Build messages
    messages = []
    model_info = None
    
    # Extract model from inputState
    input_state = state.get('inputState', {})
    if isinstance(input_state, dict):
        selected_model = input_state.get('selectedModel', {})
        if isinstance(selected_model, dict):
            model_meta = selected_model.get('metadata', {})
            if isinstance(model_meta, dict):
                model_info = model_meta.get('id') or model_meta.get('name')
    
    for req_idx, request in enumerate(requests):
        if not isinstance(request, dict):
            continue
        
        # User message
        user_text = _extract_user_message(request)
        if not user_text:
            continue
        
        # Timestamp from request
        req_ts = request.get('timestamp', 0)
        if req_ts:
            ts_str = datetime.fromtimestamp(req_ts / 1000, tz=timezone.utc).isoformat()
        elif creation_date:
            ts_str = datetime.fromtimestamp(creation_date / 1000, tz=timezone.utc).isoformat()
        else:
            ts_str = None
        
        messages.append({
            'role': 'user',
            'content': user_text,
            'msg_type': 'user_message',
            'timestamp': ts_str,
        })
        
        # AI response
        response = request.get('response', [])
        if isinstance(response, list) and response:
            ai_text = _extract_text_from_response(response)
            if ai_text:
                # Get response timing
                result = request.get('result', {})
                resp_ts = ts_str
                if isinstance(result, dict):
                    timings = result.get('timings', {})
                    if isinstance(timings, dict):
                        elapsed = timings.get('totalElapsed', 0)
                        if elapsed and req_ts:
                            resp_ts = datetime.fromtimestamp(
                                (req_ts + elapsed) / 1000, tz=timezone.utc
                            ).isoformat()
                
                messages.append({
                    'role': 'assistant',
                    'content': ai_text,
                    'msg_type': 'assistant_message',
                    'timestamp': resp_ts,
                })
        
        # Error info
        result = request.get('result', {})
        if isinstance(result, dict) and 'errorDetails' in result:
            err = result['errorDetails']
            if isinstance(err, dict):
                err_msg = err.get('message', '')
                if err_msg:
                    messages.append({
                        'role': 'system',
                        'content': f"[Error] {err_msg}",
                        'msg_type': 'error',
                        'timestamp': ts_str,
                    })
    
    if not messages:
        return None
    
    # Build session
    composite_id = f"vscode-chat-{server_name}-{session_uuid}"
    
    start_time = None
    end_time = None
    if creation_date:
        start_time = datetime.fromtimestamp(creation_date / 1000, tz=timezone.utc).isoformat()
    
    timestamps = [m['timestamp'] for m in messages if m.get('timestamp')]
    if timestamps:
        if not start_time:
            start_time = min(timestamps)
        end_time = max(timestamps)
    
    project_name = os.path.basename(workspace_folder) if workspace_folder else ''
    
    return {
        'session': {
            'id': composite_id,
            'source': 'vscode-chat',
            'server_name': server_name,
            'title': title,
            'cwd': workspace_folder or None,
            'model': model_info,
            'cli_version': None,
            'start_time': start_time,
            'end_time': end_time,
            'message_count': len(messages),
            'metadata': {
                'session_uuid': session_uuid,
                'workspace_folder': workspace_folder,
                'project': project_name,
                'initial_location': state.get('initialLocation', ''),
            },
            'raw_path': filepath,
        },
        'messages': messages,
    }


def iter_mac_chat_sessions(sessions_dir: str, manifest_path: str,
                           server_name: str = 'mac',
                           filepaths: Optional[List[str]] = None):
    """Yield Mac chat sessions one at a time to keep peak memory bounded."""
    manifest = parse_mac_chat_manifest(manifest_path)

    # Build session_uuid -> (workspace_folder, title) mapping from manifest
    session_info = {}
    for ws_hash, ws_data in manifest.items():
        ws_uri = ws_data.get('workspace', {}).get('folder', '')
        ws_folder = _decode_workspace_folder(ws_uri)

        index = ws_data.get('index', {})
        for sid, info in index.items():
            session_info[sid] = {
                'workspace_folder': ws_folder,
                'title': info.get('title', ''),
            }

        for sess in ws_data.get('sessions', []):
            fname = sess.get('file', '')
            uuid = fname.replace('.jsonl', '')
            if uuid not in session_info:
                session_info[uuid] = {
                    'workspace_folder': ws_folder,
                    'title': '',
                }

    jsonl_files = (
        sorted(filepaths)
        if filepaths is not None
        else sorted(glob_mod.glob(os.path.join(sessions_dir, '*.jsonl')))
    )
    logger.info(f"Parsing {len(jsonl_files)} changed Mac chat session files...")
    parsed_count = 0
    for filepath in jsonl_files:
        uuid = os.path.splitext(os.path.basename(filepath))[0]
        info = session_info.get(uuid, {})
        try:
            result = parse_mac_chat_session(
                filepath,
                session_id=uuid,
                workspace_folder=info.get('workspace_folder', ''),
                title=info.get('title', ''),
                server_name=server_name,
            )
            if result:
                parsed_count += 1
                yield result
        except Exception as exc:
            logger.error(f"Error parsing Mac chat session {uuid}: {exc}")
    logger.info(f"Successfully parsed {parsed_count} Mac chat sessions with messages")


def parse_all_mac_chat_sessions(sessions_dir: str, manifest_path: str,
                                server_name: str = 'mac',
                                filepaths: Optional[List[str]] = None) -> List[Dict]:
    """Parse selected Mac VS Code Chat Session files.

    Args:
        sessions_dir: Directory containing the flat .jsonl files
        manifest_path: Path to the mac_chat_manifest.json
        server_name: Server name tag

    Returns:
        List of dicts with 'session' and 'messages' keys.
    """
    return list(iter_mac_chat_sessions(
        sessions_dir,
        manifest_path,
        server_name=server_name,
        filepaths=filepaths,
    ))
