"""SQLite database with FTS5 full-text search (trigram tokenizer for CJK support)."""
import sqlite3
import os
import json
import shlex
import math
import html
import re
import threading
from typing import List, Optional, Tuple
from contextlib import contextmanager
from app.config import get_config
from app.tenant import LEGACY_DATA_SCOPE, get_data_scope


def _extract_uuid_from_session_id(session_id: str) -> Optional[str]:
    """Extract a UUID from the tail of a session id like 'codex-local-<uuid>'.

    Returns the candidate string only if it looks like a standard 36-char
    UUID with 4 dashes; otherwise None.
    """
    if not session_id or "-" not in session_id:
        return None
    parts = session_id.split("-")
    if len(parts) < 5:
        return None
    candidate = "-".join(parts[-5:])
    if len(candidate) != 36 or candidate.count("-") != 4:
        return None
    # Each segment length check: 8-4-4-4-12
    seg_lens = [len(s) for s in candidate.split("-")]
    if seg_lens != [8, 4, 4, 4, 12]:
        return None
    return candidate


def build_resume_command(source: str, session_id: str,
                         cwd: Optional[str]) -> Optional[str]:
    """Build a CLI command users can paste to resume an agent session.

    UUID-based agents extract their upstream UUID from the composite id;
    OpenCode preserves its native ``ses_*`` identifier.
    """
    if source == "opencode":
        match = re.search(r"(ses_[A-Za-z0-9]+)$", session_id or "")
        if not match:
            return None
        cmd = f"opencode --session {shlex.quote(match.group(1))}"
        return f"cd {shlex.quote(cwd)} && {cmd}" if cwd else cmd

    uuid = _extract_uuid_from_session_id(session_id or "")
    if not uuid:
        return None
    if source == "claude":
        cmd = f"claude --resume {uuid}"
    elif source == "codex":
        cmd = f"codex resume {uuid}"
    elif source == "copilot":
        # copilot --help shows `--resume[=value]`; the `=` form is canonical.
        cmd = f"copilot --resume={uuid}"
    else:
        return None
    if cwd:
        return f"cd {shlex.quote(cwd)} && {cmd}"
    return cmd

_db_path: Optional[str] = None
_db_write_generation = 0
_db_generation_lock = threading.Lock()
# API requests must fail fast enough to preserve service capacity.  A 30 second
# SQLite wait can occupy every Starlette worker token when a writer is stuck,
# turning a transient database lock into a complete API outage.
_SQLITE_BUSY_TIMEOUT_MS = max(
    250,
    int(os.environ.get("ARM_SQLITE_BUSY_TIMEOUT_MS", "5000")),
)
DEFAULT_MESSAGE_PAGE_SIZE = 100
DEFAULT_MESSAGE_PREVIEW_CHARS = 8000
SEARCH_RESULT_CONTENT_CHARS = 4000
MIN_INDEXED_QUERY_CHARS = 3
MESSAGE_VIEWS = ("all", "conversation", "assistant", "user", "process")
# Assistant tool calls and private reasoning are stored with role=assistant by
# several upstream CLIs.  Treating every assistant row as an "AI reply" makes
# that view mostly command JSON, so keep one provider-neutral classification
# here for both pagination and counts.
_ASSISTANT_PROCESS_MESSAGE_TYPES = frozenset({
    "reasoning", "tool_call", "function_call", "tool_use", "mcp_tool_call",
    "exec_command", "patch_apply", "web_search", "subtask", "error",
})
_SESSION_TAG_PREFIXES = {
    "copilot": "CPL",
    "codex": "CDX",
    "claude": "CLD",
    "opencode": "OPC",
    "vscode-chat": "VCH",
    "vscode-memory": "VME",
    "vscode-chat-edit": "VED",
    "vscode-panel": "VPN",
    "vscode-debug-log": "VDL",
}
_SEARCH_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*|[\u3400-\u9fff]+"
)
_SEARCH_FILLER_PHRASES = (
    "我记得", "我记得我", "我有一个", "有一个", "一个任务",
    "这个任务", "那个任务", "是解决", "想找", "查找", "搜索",
    "的问题", "这个问题", "那个问题", "怎么解决", "如何解决",
    "帮我", "一下", "相关", "会话", "记录", "模型",
)
_SEARCH_SEMANTIC_ALIASES = (
    {
        "label": "导出",
        "triggers": ("导出",),
        "variants": ("导出", "export"),
    },
    {
        "label": "显存",
        "triggers": ("显存",),
        "variants": ("显存", "gpu memory", "oom", "out of memory"),
    },
    {
        "label": "崩溃/OOM",
        "triggers": ("炸掉", "炸了", "爆显存", "爆了", "崩溃", "挂了"),
        "variants": (
            "炸掉", "炸了", "爆显存", "爆了", "崩溃", "挂了",
            "oom", "out of memory",
        ),
    },
)
_SEARCH_STOP_TERMS = {
    "一个", "任务", "这个", "那个", "问题", "解决", "怎么", "如何",
    "什么", "一下", "相关", "会话", "记录", "以后", "之后", "进行",
    "使用", "需要", "the", "and", "for", "with", "from", "that",
}
MAX_SEARCH_CONCEPTS = 16
MAX_SEARCH_INDEX_VARIANTS = 12
MAX_SEARCH_INDEX_CONCEPTS = 6


def is_indexed_content_query(query: str) -> bool:
    """Whether a LIKE pattern contains a literal trigram FTS can accelerate."""
    if len(query) < MIN_INDEXED_QUERY_CHARS:
        return False
    return any(
        len(segment) >= MIN_INDEXED_QUERY_CHARS
        for segment in re.split(r"[%_]+", query)
    )


def _is_assistant_reply(role: Optional[str], msg_type: Optional[str]) -> bool:
    return (
        role == "assistant"
        and (msg_type or "").casefold() not in _ASSISTANT_PROCESS_MESSAGE_TYPES
    )


def _message_matches_view(
    role: Optional[str], msg_type: Optional[str], message_view: str
) -> bool:
    if message_view == "assistant":
        return _is_assistant_reply(role, msg_type)
    if message_view == "user":
        return role == "user"
    if message_view == "conversation":
        return role == "user" or _is_assistant_reply(role, msg_type)
    if message_view == "process":
        return not (role == "user" or _is_assistant_reply(role, msg_type))
    return True


def _message_view_filter(message_view: str) -> tuple[str, list[str]]:
    """Return a SQL predicate matching the provider-neutral message view."""
    message_view = message_view if message_view in MESSAGE_VIEWS else "all"
    placeholders = ",".join("?" for _ in _ASSISTANT_PROCESS_MESSAGE_TYPES)
    reply = (
        "(role='assistant' AND lower(COALESCE(msg_type,'')) "
        f"NOT IN ({placeholders}))"
    )
    params = sorted(_ASSISTANT_PROCESS_MESSAGE_TYPES)
    if message_view == "assistant":
        return reply, params
    if message_view == "user":
        return "role='user'", []
    if message_view == "conversation":
        return f"(role='user' OR {reply})", params
    if message_view == "process":
        return f"NOT (role='user' OR {reply})", params
    return "1=1", []


def build_session_tag(session_id: str, source: Optional[str] = None) -> str:
    if not session_id:
        return ""
    prefix = _SESSION_TAG_PREFIXES.get(source or "", "SES")
    compact = "".join(ch for ch in session_id if ch.isalnum())
    if not compact:
        compact = session_id.replace("-", "")
    return f"{prefix}-{compact[-12:].upper()}"


def _with_session_tag(row: dict) -> dict:
    data = dict(row)
    session_id = data.get("session_id") or data.get("id") or ""
    if session_id and not data.get("session_tag"):
        data["session_tag"] = build_session_tag(session_id, data.get("source"))
    return data


def _normalize_search_token(value: str) -> str:
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())


def _technical_token_variants(token: str) -> list[str]:
    lowered = token.casefold()
    variants = [lowered]
    if any(separator in lowered for separator in (".", "_", "-")):
        for separator in (".", "_", "-"):
            variant = re.sub(r"[._-]", separator, lowered)
            if variant not in variants:
                variants.append(variant)
    return variants


def _parse_search_concepts(query: str) -> list[dict]:
    """Split remembered-task prose into searchable technical concepts.

    ASCII model/config identifiers stay intact and separator variants are
    treated as one concept (``qwen3.5`` also finds ``qwen3_5``). Chinese text
    is reduced to overlapping bi/tri-grams so small wording differences such
    as ``显存炸掉`` versus ``显存炸了`` still share useful search anchors.
    """
    semantic_text = query or ""
    cleaned = semantic_text
    for phrase in sorted(_SEARCH_FILLER_PHRASES, key=len, reverse=True):
        cleaned = cleaned.replace(phrase, " ")
    matched_aliases = []
    for alias in _SEARCH_SEMANTIC_ALIASES:
        if any(trigger.casefold() in semantic_text.casefold()
               for trigger in alias["triggers"]):
            matched_aliases.append(alias)
            for trigger in alias["triggers"]:
                if not trigger.isascii():
                    cleaned = cleaned.replace(trigger, " ")

    concepts: list[dict] = []
    seen = set()

    def add(label: str, variants: list[str] | None = None):
        normalized = label.casefold().strip()
        if (
            len(normalized) < 2
            or normalized in _SEARCH_STOP_TERMS
            or normalized in seen
            or len(concepts) >= MAX_SEARCH_CONCEPTS
        ):
            return
        seen.add(normalized)
        concepts.append({
            "label": label,
            "variants": variants or [label],
        })

    tokens = _SEARCH_TOKEN_RE.findall(cleaned)
    # Technical identifiers carry the most intent, so reserve capacity for
    # them before adding Chinese n-grams.
    for token in tokens:
        if token.isascii():
            add(token, _technical_token_variants(token))

    for alias in matched_aliases:
        add(alias["label"], list(alias["variants"]))

    for token in tokens:
        if token.isascii():
            continue
        if len(token) <= 3:
            add(token)
            continue
        for width in (2, 3):
            for index in range(len(token) - width + 1):
                add(token[index:index + width])
    return concepts


def _session_matches_search(row: sqlite3.Row, query: str) -> bool:
    lowered_query = (query or "").casefold()
    normalized_query = _normalize_search_token(query)
    text_fields = [
        row["id"],
        row["title"],
        row["cwd"],
        row["raw_path"],
    ]
    if any(lowered_query in (field or "").casefold() for field in text_fields):
        return True

    session_tag = build_session_tag(row["id"], row["source"])
    if lowered_query in session_tag.casefold():
        return True
    if normalized_query and normalized_query in _normalize_search_token(session_tag):
        return True
    return False


def _session_search_filters(source: str = None, server: str = None,
                            date_from: str = None, date_to: str = None) -> tuple[str, list]:
    where_clauses = []
    params: list = []
    if source:
        where_clauses.append("source = ?")
        params.append(source)
    if server:
        where_clauses.append("server_name = ?")
        params.append(server)
    if date_from:
        where_clauses.append("start_time >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("start_time <= ?")
        params.append(date_to)
    where = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    return where, params


def _find_matching_session_ids(conn, query: str, source: str = None, server: str = None,
                               date_from: str = None, date_to: str = None) -> list[str]:
    if not query:
        return []
    where, params = _session_search_filters(source, server, date_from, date_to)
    rows = conn.execute(
        f"""SELECT id, source, title, cwd, raw_path
            FROM sessions{where}
            ORDER BY COALESCE(end_time, start_time) DESC""",
        params,
    ).fetchall()
    return [row["id"] for row in rows if _session_matches_search(row, query)]


def get_base_db_path() -> str:
    """Return the legacy database path used as the tenant storage root."""
    global _db_path
    if _db_path is None:
        cfg = get_config()
        _db_path = cfg.database.path
        os.makedirs(os.path.dirname(os.path.abspath(_db_path)), exist_ok=True)
    return _db_path


def get_db_path() -> str:
    """Return the database owned by the active authenticated data scope."""
    base_path = os.path.abspath(get_base_db_path())
    scope = get_data_scope()
    if scope == LEGACY_DATA_SCOPE:
        return base_path
    tenant_path = os.path.join(
        os.path.dirname(base_path),
        "users",
        scope,
        os.path.basename(base_path),
    )
    tenant_dir = os.path.dirname(tenant_path)
    os.makedirs(tenant_dir, exist_ok=True)
    os.chmod(tenant_dir, 0o700)
    return tenant_path


def get_db_generation() -> int:
    """Monotonic in-process generation used to invalidate read caches."""
    with _db_generation_lock:
        return _db_write_generation


def _advance_db_generation() -> None:
    global _db_write_generation
    with _db_generation_lock:
        _db_write_generation += 1


def _secure_db_files() -> None:
    """Keep the database and WAL sidecars private because they may hold API keys."""
    path = get_db_path()
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:
            # WAL/SHM sidecars can disappear between any existence check and
            # chmod.  This benign SQLite race must never kill a background
            # worker after its transaction has already completed.
            continue

@contextmanager
def get_db(busy_timeout_ms: Optional[int] = None):
    timeout_ms = (
        _SQLITE_BUSY_TIMEOUT_MS
        if busy_timeout_ms is None
        else max(250, int(busy_timeout_ms))
    )
    conn = sqlite3.connect(get_db_path(), timeout=timeout_ms / 1000.0)
    conn.row_factory = sqlite3.Row
    # WAL mode is persistent on the database file.  Reissuing journal_mode on
    # every request asks SQLite for a lock during connection setup and can make
    # unrelated readers queue behind a writer.  init_db() enables it once.
    conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32768")
    conn.execute("PRAGMA mmap_size=268435456")
    try:
        yield conn
        if conn.in_transaction:
            conn.commit()
        if conn.total_changes > 0:
            _advance_db_generation()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        _secure_db_files()

def init_db():
    _secure_db_files()
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                host TEXT NOT NULL,
                port INTEGER DEFAULT 22,
                user TEXT,
                password TEXT,
                copilot_dir TEXT,
                codex_dir TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                server_name TEXT,
                title TEXT,
                cwd TEXT,
                model TEXT,
                cli_version TEXT,
                start_time TEXT,
                end_time TEXT,
                message_count INTEGER DEFAULT 0,
                metadata_json TEXT,
                raw_path TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT,
                msg_type TEXT,
                timestamp TEXT,
                metadata_json TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_messages_session_timestamp
                ON messages(session_id, timestamp, id);
            CREATE INDEX IF NOT EXISTS idx_messages_role
                ON messages(role);
            CREATE INDEX IF NOT EXISTS idx_sessions_source
                ON sessions(source);
            CREATE INDEX IF NOT EXISTS idx_sessions_server
                ON sessions(server_name);
            CREATE INDEX IF NOT EXISTS idx_sessions_start_time
                ON sessions(start_time);
            CREATE INDEX IF NOT EXISTS idx_sessions_activity
                ON sessions(COALESCE(end_time, start_time) DESC);

            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_name TEXT,
                source TEXT,
                status TEXT,
                sessions_synced INTEGER DEFAULT 0,
                messages_synced INTEGER DEFAULT 0,
                error_message TEXT,
                started_at TEXT DEFAULT (datetime('now')),
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_tags (
                session_id TEXT REFERENCES sessions(id),
                tag_id INTEGER REFERENCES tags(id),
                PRIMARY KEY (session_id, tag_id)
            );
        """)
        _ensure_fts(conn)
        # Additive derived-data schema.  Import here avoids a module cycle:
        # work_memory uses get_db but raw conversation storage remains usable
        # on its own.
        from app.work_memory import init_work_memory_schema
        init_work_memory_schema(conn)
    _secure_db_files()

def _ensure_fts(conn):
    """Ensure compact trigram FTS5 tables exist.

    Existing trigram indexes remain usable and are not rebuilt during startup.
    Rebuilding a multi-gigabyte index belongs in an explicit maintenance window.
    Fresh/rebuilt indexes omit token positions and doc sizes; this is much
    smaller and still supports the substring LIKE queries used by this app.
    """
    need_rebuild = False
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages_fts'"
        ).fetchone()
        if row:
            if 'trigram' not in row[0]:
                need_rebuild = True
        else:
            need_rebuild = True
    except sqlite3.Error:
        need_rebuild = True

    if need_rebuild:
        conn.executescript("""
            DROP TABLE IF EXISTS messages_fts;
            DROP TRIGGER IF EXISTS messages_ai;
            DROP TRIGGER IF EXISTS messages_ad;
            DROP TRIGGER IF EXISTS messages_au;

            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content,
                msg_type,
                session_id UNINDEXED,
                role UNINDEXED,
                content=messages,
                content_rowid=id,
                tokenize='trigram',
                detail='none',
                columnsize=0
            );

            CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content, msg_type, session_id, role)
                VALUES (new.id, new.content, new.msg_type, new.session_id, new.role);
            END;

            CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, msg_type, session_id, role)
                VALUES ('delete', old.id, old.content, old.msg_type, old.session_id, old.role);
            END;

            CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, msg_type, session_id, role)
                VALUES ('delete', old.id, old.content, old.msg_type, old.session_id, old.role);
                INSERT INTO messages_fts(rowid, content, msg_type, session_id, role)
                VALUES (new.id, new.content, new.msg_type, new.session_id, new.role);
            END;
        """)
        # Backfill existing messages
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if count > 0:
            conn.execute("""
                INSERT INTO messages_fts(rowid, content, msg_type, session_id, role)
                SELECT id, content, msg_type, session_id, role FROM messages
            """)
            conn.commit()

    # Session FTS for title search
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='sessions_fts'"
        ).fetchone()
        if row and 'trigram' in row[0]:
            return
    except sqlite3.Error:
        pass

    conn.executescript("""
        DROP TABLE IF EXISTS sessions_fts;
        DROP TRIGGER IF EXISTS sessions_fts_ai;
        DROP TRIGGER IF EXISTS sessions_fts_ad;
        DROP TRIGGER IF EXISTS sessions_fts_au;

        CREATE VIRTUAL TABLE sessions_fts USING fts5(
            title,
            cwd,
            id UNINDEXED,
            content=sessions,
            content_rowid=rowid,
            tokenize='trigram',
            detail='none',
            columnsize=0
        );

        CREATE TRIGGER sessions_fts_ai AFTER INSERT ON sessions BEGIN
            INSERT INTO sessions_fts(rowid, title, cwd, id)
            VALUES (new.rowid, new.title, new.cwd, new.id);
        END;

        CREATE TRIGGER sessions_fts_ad AFTER DELETE ON sessions BEGIN
            INSERT INTO sessions_fts(sessions_fts, rowid, title, cwd, id)
            VALUES ('delete', old.rowid, old.title, old.cwd, old.id);
        END;

        CREATE TRIGGER sessions_fts_au AFTER UPDATE ON sessions BEGIN
            INSERT INTO sessions_fts(sessions_fts, rowid, title, cwd, id)
            VALUES ('delete', old.rowid, old.title, old.cwd, old.id);
            INSERT INTO sessions_fts(rowid, title, cwd, id)
            VALUES (new.rowid, new.title, new.cwd, new.id);
        END;
    """)
    count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if count > 0:
        conn.execute("""
            INSERT INTO sessions_fts(rowid, title, cwd, id)
            SELECT rowid, title, cwd, id FROM sessions
        """)
        conn.commit()

# ---- Query helpers ----

_SESSION_UPDATE_FIELDS = (
    ("source", "source"),
    ("server_name", "server_name"),
    ("title", "title"),
    ("cwd", "cwd"),
    ("model", "model"),
    ("cli_version", "cli_version"),
    ("start_time", "start_time"),
    ("end_time", "end_time"),
    ("message_count", "message_count"),
    ("raw_path", "raw_path"),
)


def _upsert_session_conn(conn, session_data: dict) -> bool:
    existing = conn.execute(
        "SELECT id FROM sessions WHERE id = ?", (session_data["id"],)
    ).fetchone()
    if existing:
        clauses = []
        params = []
        for key, column in _SESSION_UPDATE_FIELDS:
            if key not in session_data:
                continue
            clauses.append(f"{column}=?")
            params.append(session_data[key])
        if "metadata" in session_data:
            clauses.append("metadata_json=?")
            metadata = session_data.get("metadata")
            params.append(
                json.dumps(metadata, ensure_ascii=False)
                if metadata is not None else None
            )
        if clauses:
            clauses.append("updated_at=datetime('now')")
            params.append(session_data["id"])
            conn.execute(
                f"UPDATE sessions SET {', '.join(clauses)} WHERE id=?", params
            )
        return False

    metadata = session_data.get("metadata")
    conn.execute("""
        INSERT INTO sessions (id, source, server_name, title, cwd, model,
            cli_version, start_time, end_time, message_count, metadata_json, raw_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_data["id"], session_data.get("source", "unknown"),
        session_data.get("server_name"), session_data.get("title"),
        session_data.get("cwd"), session_data.get("model"),
        session_data.get("cli_version"), session_data.get("start_time"),
        session_data.get("end_time"), session_data.get("message_count", 0),
        json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
        session_data.get("raw_path")
    ))
    return True


def upsert_session(session_data: dict) -> bool:
    """Insert or update supplied session fields. Returns True if new."""
    with get_db() as conn:
        return _upsert_session_conn(conn, session_data)

def _message_db_values(session_id: str, message: dict) -> tuple:
    metadata = message.get("metadata")
    return (
        session_id,
        message.get("role", "unknown"),
        message.get("content"),
        message.get("msg_type"),
        message.get("timestamp"),
        json.dumps(metadata, ensure_ascii=False) if metadata else None,
    )


def _source_message_key(msg_type: Optional[str], metadata_json: Optional[str]) -> Optional[str]:
    if not metadata_json:
        return None
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    part_id = metadata.get("opencode_part_id")
    if part_id:
        return f"part:{part_id}:{msg_type or ''}"
    message_id = metadata.get("opencode_message_id")
    if message_id:
        return f"message:{message_id}:{msg_type or ''}"
    return None


def _capture_evidence_source_keys(conn, session_id: str) -> list[tuple[int, str]]:
    try:
        rows = conn.execute(
            """SELECT e.id, m.msg_type, m.metadata_json
               FROM work_item_evidence e
               JOIN messages m ON m.id=e.message_id
               WHERE e.session_id=?""",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    result = []
    for row in rows:
        key = _source_message_key(row["msg_type"], row["metadata_json"])
        if key:
            result.append((row["id"], key))
    return result


def _restore_evidence_source_keys(conn, session_id: str,
                                  evidence_keys: list[tuple[int, str]]) -> None:
    if not evidence_keys:
        return
    rows = conn.execute(
        """SELECT id, msg_type, metadata_json FROM messages
           WHERE session_id=?""",
        (session_id,),
    ).fetchall()
    message_ids = {}
    for row in rows:
        key = _source_message_key(row["msg_type"], row["metadata_json"])
        if key:
            message_ids.setdefault(key, row["id"])
    for evidence_id, key in evidence_keys:
        message_id = message_ids.get(key)
        if message_id:
            conn.execute(
                "UPDATE work_item_evidence SET message_id=? WHERE id=?",
                (message_id, evidence_id),
            )


def _messages_are_append_only(conn, session_id: str, messages: List[dict],
                              existing_count: int) -> bool:
    """Verify that an updated source kept its complete existing message prefix."""
    if existing_count <= 0 or len(messages) < existing_count:
        return False
    rows = conn.execute(
        """SELECT role,content,msg_type,timestamp,metadata_json FROM messages
           WHERE session_id=? ORDER BY id""",
        (session_id,),
    )
    for offset, row in enumerate(rows):
        expected = _message_db_values(session_id, messages[offset])
        if (
            row["role"],
            row["content"],
            row["msg_type"],
            row["timestamp"],
            row["metadata_json"],
        ) != expected[1:]:
            return False
    return offset + 1 == existing_count


def _reset_enrichment_progress(conn, session_id: str) -> None:
    """Discard a derived cursor when the raw conversation was rewritten."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrichment_jobs'"
    ).fetchone():
        return
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(enrichment_jobs)")
    }
    cursor_assignment = (
        ",processed_message_count=0"
        if "processed_message_count" in columns else ""
    )
    conn.execute(
        f"""UPDATE enrichment_jobs SET processed_hash=NULL,attempts=0,
                   error_message=NULL{cursor_assignment}
            WHERE session_id=?""",
        (session_id,),
    )


def insert_messages(session_id: str, messages: List[dict], force: bool = False,
                    incremental: bool = False) -> int:
    """Insert or refresh a session's messages.

    ``incremental=True`` turns the common growing-log case into an append,
    so only new rows are indexed. Explicit force reimports still replace all
    rows. Returns the number of inserted rows.
    """
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,)
        ).fetchone()[0]
        if count > 0:
            if not force:
                return 0
            if incremental and _messages_are_append_only(
                conn, session_id, messages, count
            ):
                new_messages = messages[count:]
                if not new_messages:
                    return 0
                conn.executemany("""
                    INSERT INTO messages
                        (session_id, role, content, msg_type, timestamp, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    _message_db_values(session_id, message)
                    for message in new_messages
                ))
                _mark_session_for_enrichment(conn, session_id, "messages_appended")
                return len(new_messages)
            _reset_enrichment_progress(conn, session_id)
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.executemany("""
            INSERT INTO messages (session_id, role, content, msg_type, timestamp, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (_message_db_values(session_id, message) for message in messages))
        if messages:
            _mark_session_for_enrichment(conn, session_id, "messages_imported")
        return len(messages)


def replace_session_with_messages(session_data: dict,
                                  messages: List[dict]) -> bool:
    """Atomically refresh a mutable source session and preserve evidence links."""
    session_id = session_data["id"]
    with get_db() as conn:
        is_new = not conn.execute(
            "SELECT 1 FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        append_only = bool(
            existing_count
            and _messages_are_append_only(
                conn, session_id, messages, existing_count
            )
        )
        evidence_keys = _capture_evidence_source_keys(conn, session_id)
        _upsert_session_conn(conn, session_data)
        if existing_count and not append_only:
            _reset_enrichment_progress(conn, session_id)
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        conn.executemany("""
            INSERT INTO messages
                (session_id, role, content, msg_type, timestamp, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (_message_db_values(session_id, message) for message in messages))
        _restore_evidence_source_keys(conn, session_id, evidence_keys)
        if messages:
            _mark_session_for_enrichment(conn, session_id, "messages_imported")
        return is_new


def _mark_session_for_enrichment(conn, session_id: str, reason: str) -> None:
    """Best-effort bridge from raw ingestion to the derived job queue."""
    try:
        from app.config import get_config
        if not get_config().agent.auto_enrich:
            return
        from app.work_memory import mark_session_pending
        mark_session_pending(conn, session_id, reason=reason, priority=10)
    except sqlite3.Error:
        # Raw sync/search must never fail merely because the optional derived
        # intelligence layer is being migrated or repaired.
        return

def search_messages(query: str, limit: int = 50, offset: int = 0,
                   source: str = None, server: str = None,
                   date_from: str = None, date_to: str = None) -> List[dict]:
    """Enhanced search: FTS5 trigram first, LIKE fallback."""
    with get_db() as conn:
        # Session metadata is tiny and includes generated tags that are not in
        # FTS. Message matches are queried separately through the trigram index.
        matched_session_ids = _find_matching_session_ids(
            conn, query, source, server, date_from, date_to
        )
        results = _fts_search(conn, query, limit, offset, source, server, date_from, date_to)
        session_results = _session_match_rows(conn, matched_session_ids, limit, offset)
        if results is None:
            results = _like_search(
                conn,
                query,
                limit,
                offset,
                source,
                server,
                date_from,
                date_to,
                None,
            )
        combined = []
        seen_results = set()
        # Merge both result types chronologically. Concatenating all metadata
        # matches first can completely hide newer full-text hits, while doing
        # the reverse can hide a newer title/tag match.
        for row in results + session_results:
            message_id = row.get("id")
            dedupe_key = (
                ("message", message_id)
                if message_id is not None
                else ("session", row.get("session_id"))
            )
            if dedupe_key in seen_results:
                continue
            seen_results.add(dedupe_key)
            combined.append(row)
        combined.sort(
            key=lambda row: (
                row.get("timestamp")
                or row.get("end_time")
                or row.get("start_time")
                or ""
            ),
            reverse=True,
        )
        return [_with_session_tag(r) for r in combined[:limit]]


def search_sessions(query: str, limit: int = 50, offset: int = 0,
                    source: str = None, server: str = None,
                    date_from: str = None,
                    date_to: str = None,
                    match_mode: str = "expanded") -> Tuple[List[dict], int]:
    """Return one result per matching session, with an exact total.

    A conversation can mention the same term thousands of times. Returning
    individual messages lets a few long conversations consume the entire
    result page. This view groups content hits by session, keeps the newest
    matching message as its preview, and reports the number of matching
    messages in that session.
    """
    with get_db() as conn:
        matched_metadata_ids = _find_matching_session_ids(
            conn, query, source, server, date_from, date_to
        )
        if match_mode == "precise":
            content_results, concepts = _precise_content_search_for_query(
                conn, query, source, server, date_from, date_to
            )
        else:
            content_results, concepts = _content_search_for_query(
                conn, query, source, server, date_from, date_to
            )
        metadata_results = _session_match_rows(
            conn,
            matched_metadata_ids,
            len(matched_metadata_ids),
            0,
        )

        by_session = {
            row["session_id"]: row
            for row in content_results
        }
        for row in metadata_results:
            session_id = row["session_id"]
            if session_id in by_session:
                by_session[session_id]["metadata_match"] = True
                by_session[session_id]["relevance_score"] = (
                    by_session[session_id].get("relevance_score", 0) + 10
                )
            else:
                row["matched_terms"] = ["标题/路径"]
                row["term_count"] = max(1, len(concepts))
                row["term_coverage"] = max(1, len(concepts))
                row["relevance_score"] = len(concepts) * 100 + 5
                row["display_title"] = row.get("title") or (
                    os.path.basename((row.get("cwd") or "").rstrip("/"))
                    or "匹配会话"
                )
                by_session[session_id] = row

        combined = list(by_session.values())
        combined.sort(
            key=lambda row: (
                row.get("relevance_score", 0) if len(concepts) > 1 else 0,
                row.get("end_time")
                or row.get("start_time")
                or row.get("timestamp")
                or ""
            ),
            reverse=True,
        )
        total = len(combined)
        page = combined[offset:offset + limit]
        return [_with_session_tag(row) for row in page], total


def _grouped_content_search(conn, query, source=None, server=None,
                            date_from=None, date_to=None):
    """Find content matches grouped to one representative row per session."""
    if not is_indexed_content_query(query):
        return []
    try:
        return _execute_grouped_content_search(
            conn, query, source, server, date_from, date_to, use_fts=True
        )
    except sqlite3.OperationalError:
        return _execute_grouped_content_search(
            conn, query, source, server, date_from, date_to, use_fts=False
        )


def _precise_content_search_for_query(
    conn, query, source=None, server=None, date_from=None, date_to=None
):
    """Fast first paint: search the remembered phrase before synonym expansion."""
    concepts = _parse_search_concepts(query)
    rows = _grouped_content_search(
        conn, query, source, server, date_from, date_to
    )
    for row in rows:
        content = row.get("content") or ""
        lowered = content.casefold()
        matched = [
            concept["label"] for concept in concepts
            if any(
                str(variant).casefold() in lowered
                for variant in concept.get("variants") or []
            )
        ]
        row["matched_terms"] = matched
        row["term_count"] = len(concepts)
        row["term_coverage"] = len(matched)
        row["message_coverage"] = len(matched)
        row["role_priority"] = {"user": 3, "assistant": 2}.get(row.get("role"), 1)
        row["relevance_score"] = len(matched) * 100 + row["role_priority"] * 5
        row["display_title"] = _derive_search_display_title(
            content, concepts, row.get("cwd")
        )
    return rows, concepts


def _content_search_for_query(conn, query, source=None, server=None,
                              date_from=None, date_to=None):
    concepts = _parse_search_concepts(query)
    indexed_concepts = []
    indexed_variant_count = 0
    indexed_ascii_concepts = sum(
        concept["label"].isascii()
        and any(is_indexed_content_query(item) for item in concept["variants"])
        for concept in concepts
    )
    for index, concept in enumerate(concepts):
        if indexed_ascii_concepts >= 2 and not concept["label"].isascii():
            continue
        variants = []
        for variant in concept["variants"]:
            if (
                is_indexed_content_query(variant)
                and variant.casefold() not in {
                    item.casefold() for item in variants
                }
                and indexed_variant_count < MAX_SEARCH_INDEX_VARIANTS
            ):
                variants.append(variant)
                indexed_variant_count += 1
        if variants:
            indexed_concepts.append((index, variants))
        if len(indexed_concepts) >= MAX_SEARCH_INDEX_CONCEPTS:
            break
    if indexed_concepts:
        try:
            return _smart_grouped_content_search(
                conn,
                concepts,
                indexed_concepts,
                source,
                server,
                date_from,
                date_to,
            ), concepts
        except sqlite3.OperationalError:
            # Keep exact search available if a damaged/legacy FTS table cannot
            # execute the richer query.
            pass
    return _grouped_content_search(
        conn, query, source, server, date_from, date_to
    ), concepts


def _smart_grouped_content_search(conn, concepts, indexed_concepts,
                                  source=None, server=None,
                                  date_from=None, date_to=None):
    conn.execute("""
        CREATE TEMP TABLE IF NOT EXISTS search_query_hits (
            message_id INTEGER NOT NULL,
            concept_id INTEGER NOT NULL,
            PRIMARY KEY (message_id, concept_id)
        ) WITHOUT ROWID
    """)
    conn.execute("DELETE FROM search_query_hits")
    indexed_ids = []
    for concept_id, variants in indexed_concepts:
        indexed_ids.append(concept_id)
        conditions = ["messages_fts.content LIKE ?" for _ in variants]
        sql = f"""
            INSERT OR IGNORE INTO search_query_hits(message_id, concept_id)
            SELECT messages_fts.rowid, ?
            FROM messages_fts
            CROSS JOIN messages m ON m.id = messages_fts.rowid
            CROSS JOIN sessions s ON s.id = m.session_id
            WHERE ({' OR '.join(conditions)})
        """
        params = [concept_id, *(f"%{variant}%" for variant in variants)]
        if source:
            sql += " AND s.source = ?"
            params.append(source)
        if server:
            sql += " AND s.server_name = ?"
            params.append(server)
        if date_from:
            sql += " AND s.start_time >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND s.start_time <= ?"
            params.append(date_to)
        conn.execute(sql, params)

    session_term_columns = ",\n                   ".join(
        "MAX(CASE WHEN h.concept_id = "
        f"{concept_id} THEN 1 ELSE 0 END) AS session_term_{concept_id}"
        for concept_id in indexed_ids
    )
    selected_session_terms = ",\n               ".join(
        f"ss.session_term_{concept_id}" for concept_id in indexed_ids
    )
    sql = f"""
        WITH message_hits AS (
            SELECT h.message_id AS id, m.session_id, m.role, m.timestamp,
                   m.msg_type, length(m.content) AS content_length,
                   COUNT(DISTINCT h.concept_id) AS indexed_message_coverage,
                   CASE m.role
                       WHEN 'user' THEN 3
                       WHEN 'assistant' THEN 2
                       ELSE 1
                   END AS role_priority
            FROM search_query_hits h
            JOIN messages m ON m.id = h.message_id
            GROUP BY h.message_id
        ),
        session_stats AS (
            SELECT m.session_id,
                   COUNT(DISTINCT h.message_id) AS match_count,
                   {session_term_columns}
            FROM search_query_hits h
            JOIN messages m ON m.id = h.message_id
            GROUP BY m.session_id
        ),
        ranked AS (
            SELECT mh.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY session_id
                       ORDER BY indexed_message_coverage DESC,
                                role_priority DESC,
                                CASE WHEN content_length BETWEEN 10 AND 8000
                                     THEN 0 ELSE 1 END,
                                content_length ASC,
                                id DESC
                   ) AS search_rank
            FROM message_hits mh
        )
        SELECT r.id, r.session_id, r.role,
               substr(m.content, 1, 16000) AS content,
               r.content_length,
               CASE WHEN r.content_length > {SEARCH_RESULT_CONTENT_CHARS}
                    THEN 1 ELSE 0 END AS content_truncated,
               r.timestamp, r.msg_type,
               s.source, s.server_name, s.title, s.cwd,
               s.start_time, s.end_time,
               ss.match_count, 'content' AS match_type,
               r.indexed_message_coverage, r.role_priority,
               {selected_session_terms},
               '' AS snippet
        FROM ranked r
        JOIN session_stats ss ON ss.session_id = r.session_id
        JOIN messages m ON m.id = r.id
        JOIN sessions s ON s.id = r.session_id
        WHERE r.search_rank <= 5
    """
    rows = conn.execute(sql).fetchall()
    best_by_session = {}
    for row in rows:
        result = dict(row)
        content = result.get("content") or ""
        matched_terms = [
            concept["label"]
            for index, concept in enumerate(concepts)
            if (
                result.pop(f"session_term_{index}", 0)
                or any(
                    variant.casefold() in content.casefold()
                    for variant in concept["variants"]
                )
            )
        ]
        message_coverage = sum(
            any(
                variant.casefold() in content.casefold()
                for variant in concept["variants"]
            )
            for concept in concepts
        )
        result["message_coverage"] = message_coverage
        result["matched_terms"] = matched_terms
        result["term_count"] = len(concepts)
        result["term_coverage"] = len(matched_terms)
        result["relevance_score"] = (
            len(matched_terms) * 100
            + message_coverage * 20
            + result.get("role_priority", 0) * 5
        )
        if any(
            phrase in content
            for phrase in ("搜索不", "找不到", "我记得", "想找", "搜索结果")
        ):
            # A live conversation about searching often repeats the entire
            # query and would otherwise outrank the historical task itself.
            result["relevance_score"] -= 400
            result["search_meta_penalty"] = True
        result["snippet"] = _build_multi_search_snippet(content, concepts)
        result["display_title"] = _derive_search_display_title(
            content, concepts, result.get("cwd")
        )
        result["content"] = content[:SEARCH_RESULT_CONTENT_CHARS]
        previous = best_by_session.get(result["session_id"])
        candidate_key = (
            result["relevance_score"],
            result["message_coverage"],
            result.get("role_priority", 0),
            -(result.get("content_length") or 0),
            result.get("id") or 0,
        )
        if previous is None or candidate_key > previous[0]:
            best_by_session[result["session_id"]] = (candidate_key, result)
    return [item[1] for item in best_by_session.values()]


def _execute_grouped_content_search(conn, query, source, server,
                                    date_from, date_to, use_fts):
    if use_fts:
        message_source = """messages_fts
            CROSS JOIN messages m ON m.id = messages_fts.rowid"""
        content_filter = "messages_fts.content LIKE ?"
    else:
        message_source = "messages m"
        content_filter = "m.content LIKE ?"

    sql = f"""
        WITH grouped_hits AS (
            SELECT m.session_id,
                   COALESCE(
                       MAX(CASE WHEN m.role = 'user' THEN m.id END),
                       MAX(m.id)
                   ) AS message_id,
                   COUNT(*) AS match_count
            FROM {message_source}
            CROSS JOIN sessions s ON s.id = m.session_id
            WHERE {content_filter}
    """
    params: list = [f"%{query}%"]
    if source:
        sql += " AND s.source = ?"
        params.append(source)
    if server:
        sql += " AND s.server_name = ?"
        params.append(server)
    if date_from:
        sql += " AND s.start_time >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND s.start_time <= ?"
        params.append(date_to)
    sql += """
            GROUP BY m.session_id
        )
        SELECT m.id, m.session_id, m.role,
               substr(
                   m.content,
                   max(1, instr(lower(m.content), lower(?)) - 160),
                   ?
               ) AS content,
               length(m.content) AS content_length,
               CASE WHEN length(m.content) > ? THEN 1 ELSE 0 END
                   AS content_truncated,
               m.timestamp, m.msg_type,
               s.source, s.server_name, s.title, s.cwd,
               s.start_time, s.end_time,
               h.match_count, 'content' AS match_type, '' AS snippet
        FROM grouped_hits h
        JOIN messages m ON m.id = h.message_id
        JOIN sessions s ON s.id = h.session_id
        ORDER BY COALESCE(s.end_time, s.start_time) DESC
    """
    params.extend([
        query,
        SEARCH_RESULT_CONTENT_CHARS,
        SEARCH_RESULT_CONTENT_CHARS,
    ])
    rows = conn.execute(sql, params).fetchall()
    results = []
    for row in rows:
        result = dict(row)
        result["snippet"] = _build_search_snippet(
            result.get("content") or "", query
        )
        results.append(result)
    return results


def _session_match_rows(conn, matched_session_ids, limit, offset):
    if not matched_session_ids or offset >= len(matched_session_ids):
        return []
    selected_ids = matched_session_ids[offset:offset + limit]
    results = []
    # Stay below SQLite builds with a conservative host-parameter limit.
    for chunk_start in range(0, len(selected_ids), 500):
        chunk = selected_ids[chunk_start:chunk_start + 500]
        placeholders = ",".join("?" for _ in chunk)
        sql = f"""
            SELECT m.id, s.id AS session_id, m.role,
                   substr(m.content, 1, ?) AS content,
                   length(m.content) AS content_length,
                   CASE WHEN length(m.content) > ? THEN 1 ELSE 0 END
                       AS content_truncated,
                   m.timestamp, m.msg_type,
                   s.source, s.server_name, s.title, s.cwd,
                   s.start_time, s.end_time,
                   0 AS match_count, 'session_metadata' AS match_type,
                   '' AS snippet
            FROM sessions s
            LEFT JOIN messages m ON m.id = (
                SELECT id FROM messages
                WHERE session_id = s.id
                ORDER BY id DESC
                LIMIT 1
            )
            WHERE s.id IN ({placeholders})
            ORDER BY COALESCE(s.end_time, s.start_time) DESC
        """
        rows = conn.execute(
            sql,
            [SEARCH_RESULT_CONTENT_CHARS, SEARCH_RESULT_CONTENT_CHARS, *chunk],
        ).fetchall()
        for row in rows:
            result = dict(row)
            label = (
                f"[Session Match] "
                f"{build_session_tag(result.get('session_id', ''), result.get('source'))} "
                f"{result.get('title') or ''}"
            ).strip()
            result["snippet"] = html.escape(label)
            results.append(result)
    results.sort(
        key=lambda row: row.get("end_time") or row.get("start_time") or "",
        reverse=True,
    )
    return results

def _fts_search(conn, query, limit, offset, source, server, date_from, date_to):
    # Patterns without a literal trigram make FTS5 fall back to a full scan.
    if not is_indexed_content_query(query):
        return []
    try:
        sql = """
            SELECT m.id, m.session_id, m.role,
                   substr(
                       m.content,
                       max(1, instr(lower(m.content), lower(?)) - 160),
                       ?
                   ) AS content,
                   length(m.content) AS content_length,
                   CASE WHEN length(m.content) > ? THEN 1 ELSE 0 END AS content_truncated,
                   m.timestamp, m.msg_type,
                   s.source, s.server_name, s.title, s.start_time, s.end_time,
                   '' AS snippet
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts.content LIKE ?
        """
        params: list = [
            query,
            SEARCH_RESULT_CONTENT_CHARS,
            SEARCH_RESULT_CONTENT_CHARS,
            f"%{query}%",
        ]
        if source:
            sql += " AND s.source = ?"
            params.append(source)
        if server:
            sql += " AND s.server_name = ?"
            params.append(server)
        if date_from:
            sql += " AND s.start_time >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND s.start_time <= ?"
            params.append(date_to)
        sql += " ORDER BY messages_fts.rowid DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["snippet"] = _build_search_snippet(
                item.get("content") or "", query
            )
            results.append(item)
        return results
    except sqlite3.OperationalError:
        return None


def _build_search_snippet(content: str, query: str) -> str:
    if not content:
        return ""
    lowered = content.casefold()
    index = lowered.find(query.casefold())
    if index < 0:
        return html.escape(content[:320])
    start = max(0, index - 100)
    end = min(len(content), index + len(query) + 180)
    return (
        ("..." if start else "")
        + html.escape(content[start:index])
        + "<mark>"
        + html.escape(content[index:index + len(query)])
        + "</mark>"
        + html.escape(content[index + len(query):end])
        + ("..." if end < len(content) else "")
    )


def _build_multi_search_snippet(content: str, concepts: list[dict]) -> str:
    if not content:
        return ""
    lowered = content.casefold()
    occurrences = []
    present_variants = []
    for concept in concepts:
        label = concept["label"]
        for variant in concept["variants"]:
            index = lowered.find(variant.casefold())
            if index >= 0:
                occurrences.append((index, index + len(variant), label))
                present_variants.append(variant)

    if not occurrences:
        return html.escape(content[:700])

    best_start = max(0, occurrences[0][0] - 140)
    best_score = -1
    for occurrence_start, _, _ in occurrences:
        window_start = max(0, occurrence_start - 140)
        window_end = min(len(content), window_start + 850)
        labels = {
            label
            for start, _, label in occurrences
            if window_start <= start < window_end
        }
        if len(labels) > best_score:
            best_score = len(labels)
            best_start = window_start

    best_end = min(len(content), best_start + 850)
    excerpt = content[best_start:best_end]
    unique_variants = sorted(
        {variant for variant in present_variants},
        key=len,
        reverse=True,
    )
    pattern = re.compile(
        "(" + "|".join(re.escape(item) for item in unique_variants) + ")",
        re.IGNORECASE,
    )
    pieces = []
    cursor = 0
    for match in pattern.finditer(excerpt):
        pieces.append(html.escape(excerpt[cursor:match.start()]))
        pieces.append("<mark>" + html.escape(match.group(0)) + "</mark>")
        cursor = match.end()
    pieces.append(html.escape(excerpt[cursor:]))
    return (
        ("..." if best_start else "")
        + "".join(pieces)
        + ("..." if best_end < len(content) else "")
    )


def _derive_search_display_title(content: str, concepts: list[dict],
                                 cwd: str | None) -> str:
    candidates = []
    for raw_line in content.splitlines():
        # Codex transcripts contain control wrappers such as
        # <subagent_notification> and <command-message>. They are useful in
        # the raw record but make terrible result titles, so keep their text
        # while dropping the wrapper itself.
        line = re.sub(r"</?[A-Za-z][^>]*>", " ", raw_line)
        line = " ".join(line.strip().split())
        if not 8 <= len(line) <= 220:
            continue
        lowered = line.casefold()
        coverage = sum(
            any(variant.casefold() in lowered for variant in concept["variants"])
            for concept in concepts
        )
        has_cjk = bool(re.search(r"[\u3400-\u9fff]", line))
        candidates.append((coverage, has_cjk, -len(line), line))
    if candidates:
        title = max(candidates)[-1]
        return title[:120] + ("…" if len(title) > 120 else "")
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return "匹配会话"

def _like_search(conn, query, limit, offset, source, server, date_from, date_to,
                 matched_session_ids=None):
    sql = """
        SELECT m.id, m.session_id, m.role, m.content, m.timestamp, m.msg_type,
               s.source, s.server_name, s.title, s.start_time, '' as snippet
        FROM messages m
        JOIN sessions s ON s.id = m.session_id
        WHERE m.content LIKE ?
    """
    params: list = [f"%{query}%"]
    if matched_session_ids:
        placeholders = ",".join("?" for _ in matched_session_ids)
        sql = sql.replace("WHERE m.content LIKE ?", f"WHERE (m.content LIKE ? OR m.session_id IN ({placeholders}))")
        params.extend(matched_session_ids)
    if source:
        sql += " AND s.source = ?"
        params.append(source)
    if server:
        sql += " AND s.server_name = ?"
        params.append(server)
    if date_from:
        sql += " AND s.start_time >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND s.start_time <= ?"
        params.append(date_to)
    sql += " ORDER BY m.timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        content = d.get("content", "") or ""
        idx = content.lower().find(query.lower())
        if idx >= 0:
            d["snippet"] = _build_search_snippet(content, query)
        elif matched_session_ids and d.get("session_id") in matched_session_ids:
            tag = build_session_tag(d.get("session_id", ""), d.get("source"))
            d["snippet"] = html.escape(
                f"[Session Match] {tag} {d.get('title', '')}".strip()
            )
        d["content_length"] = len(content)
        d["content_truncated"] = len(content) > SEARCH_RESULT_CONTENT_CHARS
        d["content"] = content[max(0, idx - 160):max(0, idx - 160) + SEARCH_RESULT_CONTENT_CHARS]
        results.append(d)
    return results

def get_sessions(limit: int = 50, offset: int = 0,
                source: str = None, server: str = None,
                date_from: str = None, date_to: str = None,
                search: str = None) -> Tuple[List[dict], int]:
    with get_db() as conn:
        search = search.strip() if search else None
        where, params = _session_search_filters(source, server, date_from, date_to)
        if search:
            metadata_ids = _find_matching_session_ids(
                conn, search, source, server, date_from, date_to
            )
            content_rows, concepts = _content_search_for_query(
                conn, search, source, server, date_from, date_to
            )
            content_by_session = {
                row["session_id"]: row for row in content_rows
            }
            matched_ids = set(metadata_ids)
            matched_ids.update(content_by_session)
            if not matched_ids:
                return [], 0

            # Session metadata is small enough to filter in memory. This also
            # avoids constructing a SQL IN clause with one parameter for every
            # matching conversation when a common term matches thousands.
            sql = f"""SELECT * FROM sessions{where}
                      ORDER BY COALESCE(end_time, start_time) DESC"""
            rows = [
                row for row in conn.execute(sql, params).fetchall()
                if row["id"] in matched_ids
            ]
            metadata_id_set = set(metadata_ids)
            rows.sort(
                key=lambda row: (
                    content_by_session.get(row["id"], {}).get(
                        "relevance_score",
                        len(concepts) * 100 + 5
                        if row["id"] in metadata_id_set else 0,
                    ) if len(concepts) > 1 else 0,
                    row["end_time"] or row["start_time"] or "",
                ),
                reverse=True,
            )
            total = len(rows)
            rows = rows[offset:offset + limit]
            results = []
            for row in rows:
                result = _with_session_tag(row)
                hit = content_by_session.get(row["id"])
                if hit:
                    result["search_display_title"] = hit.get("display_title")
                    result["search_matched_terms"] = hit.get("matched_terms", [])
                    result["search_term_coverage"] = hit.get("term_coverage", 0)
                    result["search_term_count"] = hit.get("term_count", 0)
                results.append(result)
            return results, total

        count_sql = f"SELECT COUNT(*) FROM sessions{where}"
        total = conn.execute(count_sql, params).fetchone()[0]
        sql = f"""SELECT * FROM sessions{where}
                  ORDER BY COALESCE(end_time, start_time) DESC LIMIT ? OFFSET ?"""
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        return [_with_session_tag(r) for r in rows], total

def get_session_detail(session_id: str, msg_offset: int = 0,
                       msg_limit: int = DEFAULT_MESSAGE_PAGE_SIZE,
                       content_limit: int = 0,
                       anchor_message_id: Optional[int] = None,
                       message_view: str = "all") -> Optional[dict]:
    with get_db() as conn:
        session = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            return None
        message_view = message_view if message_view in MESSAGE_VIEWS else "all"
        view_filter, view_params = _message_view_filter(message_view)
        distribution = conn.execute(
            """SELECT role,COALESCE(msg_type,'') AS msg_type,COUNT(*) AS count
               FROM messages WHERE session_id=? GROUP BY role,msg_type""",
            (session_id,),
        ).fetchall()
        view_counts = {view: 0 for view in MESSAGE_VIEWS}
        for group in distribution:
            count = int(group["count"] or 0)
            for view in MESSAGE_VIEWS:
                if _message_matches_view(group["role"], group["msg_type"], view):
                    view_counts[view] += count
        total_messages = view_counts[message_view]
        total_messages_unfiltered = view_counts["all"]
        if anchor_message_id is not None and msg_limit > 0:
            anchor = conn.execute(
                f"""SELECT id FROM messages
                    WHERE id=? AND session_id=? AND {view_filter}""",
                (anchor_message_id, session_id, *view_params),
            ).fetchone()
            if anchor:
                position = conn.execute(
                    f"""SELECT COUNT(*) FROM messages
                        WHERE session_id=? AND id<? AND {view_filter}""",
                    (session_id, anchor_message_id, *view_params),
                ).fetchone()[0]
                msg_offset = (position // msg_limit) * msg_limit
        if msg_limit <= 0:
            msg_offset = 0
            messages = conn.execute(
                "SELECT *, length(content) AS content_length, 0 AS content_truncated "
                f"FROM messages WHERE session_id=? AND {view_filter} ORDER BY id",
                (session_id, *view_params),
            ).fetchall()
        else:
            last_page_offset = (
                ((total_messages - 1) // msg_limit) * msg_limit
                if total_messages else 0
            )
            msg_offset = max(0, min(msg_offset, last_page_offset))
            if content_limit > 0:
                messages = conn.execute(
                    """SELECT id, session_id, role,
                              CASE WHEN length(content) > ?
                                   THEN substr(content, 1, ?)
                                   ELSE content END AS content,
                              msg_type, timestamp, metadata_json, created_at,
                              length(content) AS content_length,
                              CASE WHEN length(content) > ? THEN 1 ELSE 0 END
                                  AS content_truncated
                       FROM messages
                       WHERE session_id = ? AND """ + view_filter + """
                       ORDER BY id
                       LIMIT ? OFFSET ?""",
                    (
                        content_limit,
                        content_limit,
                        content_limit,
                        session_id,
                        *view_params,
                        msg_limit,
                        msg_offset,
                    ),
                ).fetchall()
            else:
                messages = conn.execute(
                    """SELECT *, length(content) AS content_length,
                              0 AS content_truncated
                       FROM messages
                       WHERE session_id = ? AND """ + view_filter + """
                       ORDER BY id
                       LIMIT ? OFFSET ?""",
                    (session_id, *view_params, msg_limit, msg_offset),
                ).fetchall()
        result = _with_session_tag(session)
        result["messages"] = [dict(m) for m in messages]
        result["msg_total"] = total_messages
        result["msg_total_unfiltered"] = total_messages_unfiltered
        result["message_view"] = message_view
        result["message_view_counts"] = view_counts
        result["msg_offset"] = msg_offset
        result["msg_limit"] = msg_limit
        result["msg_has_more"] = msg_limit > 0 and (msg_offset + msg_limit) < total_messages
        result["msg_has_previous"] = msg_limit > 0 and msg_offset > 0
        result["msg_start"] = msg_offset + 1 if total_messages else 0
        result["msg_end"] = min(msg_offset + len(messages), total_messages)
        result["msg_page"] = (
            (msg_offset // msg_limit) + 1 if msg_limit > 0 and total_messages else 1
        )
        result["msg_total_pages"] = (
            math.ceil(total_messages / msg_limit) if msg_limit > 0 else 1
        )
        result["content_limit"] = content_limit
        result["resume_command"] = build_resume_command(
            result.get("source"), result.get("id"), result.get("cwd")
        )
        return result


def get_message_content(session_id: str, message_id: int) -> Optional[dict]:
    """Load one full message after the user explicitly expands it."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, session_id, role, content, msg_type, timestamp,
                      metadata_json, length(content) AS content_length
               FROM messages
               WHERE id = ? AND session_id = ?""",
            (message_id, session_id),
        ).fetchone()
        return dict(row) if row else None

def get_stats() -> dict:
    with get_db() as conn:
        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        by_source = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM sessions GROUP BY source"
        ).fetchall()
        by_server = conn.execute(
            "SELECT server_name, COUNT(*) as cnt FROM sessions GROUP BY server_name"
        ).fetchall()
        by_server_source = conn.execute(
            "SELECT server_name, source, COUNT(*) as cnt FROM sessions GROUP BY server_name, source"
        ).fetchall()
        recent = conn.execute(
            "SELECT * FROM sessions ORDER BY start_time DESC LIMIT 10"
        ).fetchall()
        server_source_map = {}
        for r in by_server_source:
            srv = r["server_name"] or "local"
            server_source_map.setdefault(srv, {})[r["source"]] = r["cnt"]
        return {
            "total_sessions": total_sessions,
            "total_messages": total_messages,
            "by_source": {r["source"]: r["cnt"] for r in by_source},
            "by_server": {(r["server_name"] or "local"): r["cnt"] for r in by_server},
            "by_server_source": server_source_map,
            "recent_sessions": [_with_session_tag(r) for r in recent],
        }

def log_sync(server_name: str, source: str, status: str,
             sessions_synced: int = 0, messages_synced: int = 0,
             error_message: str = None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sync_log (server_name, source, status, sessions_synced,
                                  messages_synced, error_message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (server_name, source, status, sessions_synced,
              messages_synced, error_message))

def enrich_session_metadata(session_uuid: str, workspace_folder: str = None,
                            first_user_message: str = None):
    """Enrich existing session with VS Code metadata.

    Finds sessions matching this UUID (copilot-local-{uuid}, copilot-Server-23-{uuid}, etc.)
    and updates cwd / title / metadata_json in-place.
    """
    with get_db() as conn:
        # Find all sessions whose id ends with the given UUID
        rows = conn.execute(
            "SELECT id, cwd, title, metadata_json FROM sessions WHERE id LIKE ?",
            (f"%-{session_uuid}",)
        ).fetchall()

        for row in rows:
            sid = row["id"]
            current_cwd = row["cwd"]
            current_title = row["title"]
            current_meta_raw = row["metadata_json"]

            meta = {}
            if current_meta_raw:
                try:
                    meta = json.loads(current_meta_raw)
                except (json.JSONDecodeError, TypeError):
                    meta = {}

            updates = {}
            if workspace_folder:
                meta["workspace_folder"] = workspace_folder
                if not current_cwd:
                    updates["cwd"] = workspace_folder
            if first_user_message:
                meta["first_user_message"] = first_user_message
                if not current_title:
                    updates["title"] = first_user_message[:200].replace("\n", " ")

            new_meta = json.dumps(meta, ensure_ascii=False)
            set_clauses = ["metadata_json = ?", "updated_at = datetime('now')"]
            params: list = [new_meta]
            if "cwd" in updates:
                set_clauses.append("cwd = ?")
                params.append(updates["cwd"])
            if "title" in updates:
                set_clauses.append("title = ?")
                params.append(updates["title"])
            params.append(sid)

            conn.execute(
                f"UPDATE sessions SET {', '.join(set_clauses)} WHERE id = ?",
                params,
            )


def rebuild_fts():
    """Force rebuild all FTS indexes."""
    with get_db() as conn:
        conn.executescript("""
            DROP TABLE IF EXISTS messages_fts;
            DROP TRIGGER IF EXISTS messages_ai;
            DROP TRIGGER IF EXISTS messages_ad;
            DROP TRIGGER IF EXISTS messages_au;
            DROP TABLE IF EXISTS sessions_fts;
            DROP TRIGGER IF EXISTS sessions_fts_ai;
            DROP TRIGGER IF EXISTS sessions_fts_ad;
            DROP TRIGGER IF EXISTS sessions_fts_au;
        """)
        _ensure_fts(conn)


def compact_messages_fts(batch_size: int = 10000,
                         progress_callback=None) -> dict:
    """Online rebuild of the large message FTS index in compact mode.

    A shadow index is populated in short transactions while the application
    keeps querying the old index. Mirroring triggers capture concurrent writes.
    The final rename is atomic and normally takes only a short write lock.
    """
    batch_size = max(100, int(batch_size))
    shadow = "messages_fts_compact"
    shadow_triggers = (
        "messages_fts_compact_ai",
        "messages_fts_compact_ad",
        "messages_fts_compact_au",
    )

    with get_db() as conn:
        conn.executescript(f"""
            DROP TRIGGER IF EXISTS {shadow_triggers[0]};
            DROP TRIGGER IF EXISTS {shadow_triggers[1]};
            DROP TRIGGER IF EXISTS {shadow_triggers[2]};
            DROP TABLE IF EXISTS {shadow};

            CREATE VIRTUAL TABLE {shadow} USING fts5(
                content,
                msg_type,
                session_id UNINDEXED,
                role UNINDEXED,
                content=messages,
                content_rowid=id,
                tokenize='trigram',
                detail='none',
                columnsize=0
            );

            CREATE TRIGGER {shadow_triggers[0]} AFTER INSERT ON messages BEGIN
                INSERT INTO {shadow}(rowid, content, msg_type, session_id, role)
                VALUES (new.id, new.content, new.msg_type, new.session_id, new.role);
            END;

            CREATE TRIGGER {shadow_triggers[1]} AFTER DELETE ON messages BEGIN
                INSERT INTO {shadow}({shadow}, rowid, content, msg_type, session_id, role)
                VALUES ('delete', old.id, old.content, old.msg_type, old.session_id, old.role);
            END;

            CREATE TRIGGER {shadow_triggers[2]} AFTER UPDATE ON messages BEGIN
                INSERT INTO {shadow}({shadow}, rowid, content, msg_type, session_id, role)
                VALUES ('delete', old.id, old.content, old.msg_type, old.session_id, old.role);
                INSERT INTO {shadow}(rowid, content, msg_type, session_id, role)
                VALUES (new.id, new.content, new.msg_type, new.session_id, new.role);
            END;
        """)
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]

    indexed = 0
    last_id = 0
    while last_id < max_id:
        with get_db() as conn:
            boundary_rows = conn.execute(
                """SELECT id FROM messages
                   WHERE id > ? AND id <= ?
                   ORDER BY id LIMIT ?""",
                (last_id, max_id, batch_size),
            ).fetchall()
            if not boundary_rows:
                break
            next_last_id = boundary_rows[-1][0]
            conn.execute(f"""
                INSERT INTO {shadow}(rowid, content, msg_type, session_id, role)
                SELECT id, content, msg_type, session_id, role
                FROM messages
                WHERE id > ? AND id <= ?
                ORDER BY id
            """, (last_id, next_last_id))
            indexed += len(boundary_rows)
            last_id = next_last_id
        if progress_callback:
            progress_callback(indexed, total)

    with get_db() as conn:
        conn.execute(
            f"INSERT INTO {shadow}({shadow}) VALUES ('optimize')"
        )
        # rank=1 asks FTS5 to compare the shadow index against its external
        # content table before it becomes the live index.
        conn.execute(
            f"INSERT INTO {shadow}({shadow}, rank) "
            "VALUES ('integrity-check', 1)"
        )
        conn.commit()

        conn.execute("BEGIN IMMEDIATE")
        try:
            for trigger in shadow_triggers:
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.execute("DROP TRIGGER IF EXISTS messages_ai")
            conn.execute("DROP TRIGGER IF EXISTS messages_ad")
            conn.execute("DROP TRIGGER IF EXISTS messages_au")
            conn.execute("DROP TABLE IF EXISTS messages_fts")
            conn.execute(
                f"ALTER TABLE {shadow} RENAME TO messages_fts"
            )
            trigger_sql = (
                """CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(
                        rowid, content, msg_type, session_id, role
                    )
                    VALUES (
                        new.id, new.content, new.msg_type, new.session_id, new.role
                    );
                END""",
                """CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(
                        messages_fts, rowid, content, msg_type, session_id, role
                    )
                    VALUES (
                        'delete', old.id, old.content, old.msg_type,
                        old.session_id, old.role
                    );
                END""",
                """CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts(
                        messages_fts, rowid, content, msg_type, session_id, role
                    )
                    VALUES (
                        'delete', old.id, old.content, old.msg_type,
                        old.session_id, old.role
                    );
                    INSERT INTO messages_fts(
                        rowid, content, msg_type, session_id, role
                    )
                    VALUES (
                        new.id, new.content, new.msg_type, new.session_id, new.role
                    );
                END""",
            )
            for statement in trigger_sql:
                conn.execute(statement)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'messages_fts'"
        ).fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return {
        "messages_indexed": indexed,
        "schema": schema,
        "database_bytes": page_count * page_size,
        "reusable_free_bytes": free_pages * page_size,
    }
