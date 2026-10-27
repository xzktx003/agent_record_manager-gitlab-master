"""Local user, session, and API-token storage.

Authentication data deliberately lives outside the high-write conversation
database. Passwords use the standard-library scrypt implementation and only
random token digests are persisted, so copying either database never reveals
an active browser session or bearer token directly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.database import get_base_db_path
from app.tenant import LEGACY_DATA_SCOPE


SESSION_COOKIE_NAME = "arm_session"
CSRF_COOKIE_NAME = "arm_csrf"
_AUTH_DB_BUSY_TIMEOUT_MS = 5000
_PASSWORD_N = 2**15
_PASSWORD_R = 8
_PASSWORD_P = 3
_PASSWORD_DKLEN = 32
_PASSWORD_MAXMEM = 64 * 1024 * 1024
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_MINUTES = 15
_schema_lock = threading.Lock()
_schema_path: Optional[str] = None
_password_check_slots = threading.BoundedSemaphore(2)


class AuthError(ValueError):
    """An authentication or account-management error safe for API mapping."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retry_after = retry_after


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or _now_dt()).isoformat(timespec="seconds")


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_username(username: str) -> str:
    value = str(username or "").strip()
    if not 3 <= len(value) <= 64:
        raise AuthError("invalid_username", "用户名长度必须在 3 到 64 个字符之间。")
    if any(ord(char) < 33 for char in value) or "/" in value or "\\" in value:
        raise AuthError("invalid_username", "用户名包含不允许的字符。")
    return value


def validate_password(password: str) -> str:
    value = str(password or "")
    if len(value) < 12:
        raise AuthError("weak_password", "密码至少需要 12 个字符。")
    if len(value) > 1024:
        raise AuthError("weak_password", "密码不能超过 1024 个字符。")
    return value


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    value = validate_password(password)
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        value.encode("utf-8"),
        salt=salt,
        n=_PASSWORD_N,
        r=_PASSWORD_R,
        p=_PASSWORD_P,
        dklen=_PASSWORD_DKLEN,
        maxmem=_PASSWORD_MAXMEM,
    )
    return (
        f"scrypt${_PASSWORD_N}${_PASSWORD_R}${_PASSWORD_P}"
        f"${_b64(salt)}${_b64(digest)}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt = _b64decode(raw_salt)
        expected = _b64decode(raw_digest)
        candidate = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=salt,
            n=int(raw_n),
            r=int(raw_r),
            p=int(raw_p),
            dklen=len(expected),
            maxmem=_PASSWORD_MAXMEM,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(candidate, expected)


def get_auth_db_path() -> str:
    configured = os.environ.get("ARM_AUTH_DB", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    main_path = os.path.abspath(get_base_db_path())
    stem, extension = os.path.splitext(main_path)
    return f"{stem}.auth{extension or '.db'}"


def _secure_auth_db_files() -> None:
    path = get_auth_db_path()
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:
            continue


@contextmanager
def _auth_db():
    path = get_auth_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=_AUTH_DB_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_AUTH_DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        if conn.in_transaction:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        _secure_auth_db_files()


def initialize_auth() -> None:
    global _schema_path
    path = get_auth_db_path()
    if _schema_path == path:
        return
    with _schema_lock:
        if _schema_path == path:
            return
        with _auth_db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    normalized_username TEXT NOT NULL UNIQUE,
                    data_scope TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    last_login_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    ip_address TEXT,
                    user_agent TEXT,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                    ON auth_sessions(user_id, expires_at);
                CREATE TABLE IF NOT EXISTS auth_api_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    last_used_at TEXT,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_auth_tokens_user
                    ON auth_api_tokens(user_id, created_at DESC);
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(auth_users)")
            }
            if "data_scope" not in columns:
                conn.execute(
                    "ALTER TABLE auth_users ADD COLUMN data_scope TEXT"
                )
                rows = conn.execute("SELECT id FROM auth_users ORDER BY created_at").fetchall()
                for index, row in enumerate(rows):
                    scope = LEGACY_DATA_SCOPE if index == 0 else row["id"]
                    conn.execute(
                        "UPDATE auth_users SET data_scope=? WHERE id=?",
                        (scope, row["id"]),
                    )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_users_scope "
                    "ON auth_users(data_scope)"
                )
        _schema_path = path
    _secure_auth_db_files()


def _public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    return {
        "id": value["id"],
        "username": value["username"],
        "data_scope": value["data_scope"],
        "is_admin": bool(value["is_admin"]),
        "is_active": bool(value["is_active"]),
        "must_change_password": bool(value["must_change_password"]),
        "last_login_at": value.get("last_login_at"),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
    }


class AuthRepository:
    def user_count(self) -> int:
        initialize_auth()
        with _auth_db() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM auth_users WHERE is_active=1"
            ).fetchone()[0])

    def active_data_scopes(self) -> list[str]:
        initialize_auth()
        with _auth_db() as conn:
            rows = conn.execute(
                "SELECT data_scope FROM auth_users WHERE is_active=1 ORDER BY created_at"
            ).fetchall()
        return [str(row["data_scope"]) for row in rows]

    def create_user(
        self,
        username: str,
        password: str,
        *,
        is_admin: bool = False,
        must_change_password: bool = False,
        use_legacy_data: bool = False,
    ) -> dict[str, Any]:
        initialize_auth()
        username = validate_username(username)
        password_hash = hash_password(password)
        user_id = f"user-{uuid.uuid4().hex}"
        data_scope = LEGACY_DATA_SCOPE if use_legacy_data else user_id
        now = _iso()
        try:
            with _auth_db() as conn:
                conn.execute(
                    """INSERT INTO auth_users(
                           id,username,normalized_username,data_scope,password_hash,is_admin,
                           is_active,must_change_password,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,1,?,?,?)""",
                    (
                        user_id,
                        username,
                        username.casefold(),
                        data_scope,
                        password_hash,
                        int(is_admin),
                        int(must_change_password),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthError("username_exists", "用户名已存在。") from exc
        user = self.get_user(user_id)
        from app.tenant_data import ensure_tenant_data

        ensure_tenant_data(user["data_scope"])
        return user

    def get_user(self, user_id: str) -> dict[str, Any]:
        initialize_auth()
        with _auth_db() as conn:
            row = conn.execute(
                "SELECT * FROM auth_users WHERE id=?", (user_id,)
            ).fetchone()
        if not row:
            raise AuthError("user_not_found", "用户不存在。")
        return _public_user(row)

    def get_user_by_username(self, username: str) -> dict[str, Any]:
        initialize_auth()
        with _auth_db() as conn:
            row = conn.execute(
                "SELECT * FROM auth_users WHERE normalized_username=?",
                (str(username or "").strip().casefold(),),
            ).fetchone()
        if not row:
            raise AuthError("user_not_found", "用户不存在。")
        return _public_user(row)

    def list_users(self) -> list[dict[str, Any]]:
        initialize_auth()
        with _auth_db() as conn:
            rows = conn.execute(
                "SELECT * FROM auth_users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return [_public_user(row) for row in rows]

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        # scrypt is intentionally CPU/memory expensive. Reject excess parallel
        # checks quickly so a login burst cannot exhaust the API worker pool or
        # allocate one password-work buffer per incoming connection.
        if not _password_check_slots.acquire(blocking=False):
            raise AuthError(
                "login_busy",
                "登录请求较多，请稍后重试。",
                retry_after=2,
            )
        try:
            return self._authenticate_with_slot(username, password)
        finally:
            _password_check_slots.release()

    def _authenticate_with_slot(
        self, username: str, password: str
    ) -> dict[str, Any]:
        initialize_auth()
        normalized = str(username or "").strip().casefold()
        failure: Optional[AuthError] = None
        authenticated: Optional[dict[str, Any]] = None
        with _auth_db() as conn:
            row = conn.execute(
                "SELECT * FROM auth_users WHERE normalized_username=?",
                (normalized,),
            ).fetchone()
            if not row:
                # Match the expensive password-check path to reduce username
                # enumeration via response timing.
                hash_password(str(password or "").ljust(12, "!"), salt=b"\0" * 16)
                raise AuthError("invalid_credentials", "用户名或密码错误。")
            locked_until = _parse_time(row["locked_until"])
            if locked_until and locked_until > _now_dt():
                retry_after = max(1, int((locked_until - _now_dt()).total_seconds()))
                raise AuthError(
                    "login_locked",
                    "登录失败次数过多，请稍后再试。",
                    retry_after=retry_after,
                )
            valid = bool(row["is_active"]) and verify_password(
                password, row["password_hash"]
            )
            if not valid:
                attempts = int(row["failed_attempts"] or 0) + 1
                lock_until = None
                if attempts >= _MAX_FAILED_ATTEMPTS:
                    attempts = 0
                    lock_until = _iso(
                        _now_dt() + timedelta(minutes=_LOCKOUT_MINUTES)
                    )
                conn.execute(
                    """UPDATE auth_users SET failed_attempts=?,locked_until=?,
                              updated_at=? WHERE id=?""",
                    (attempts, lock_until, _iso(), row["id"]),
                )
                if lock_until:
                    failure = AuthError(
                        "login_locked",
                        "登录失败次数过多，请稍后再试。",
                        retry_after=_LOCKOUT_MINUTES * 60,
                    )
                else:
                    failure = AuthError(
                        "invalid_credentials", "用户名或密码错误。"
                    )
            else:
                now = _iso()
                conn.execute(
                    """UPDATE auth_users SET failed_attempts=0,locked_until=NULL,
                              last_login_at=?,updated_at=? WHERE id=?""",
                    (now, now, row["id"]),
                )
                current = dict(row)
                current["last_login_at"] = now
                current["updated_at"] = now
                authenticated = _public_user(current)
        # Raise only after the context manager commits the failed-attempt
        # counter. Raising inside the transaction would roll it back and make
        # account lockout ineffective.
        if failure is not None:
            raise failure
        if authenticated is None:  # defensive; every branch above sets one
            raise AuthError("invalid_credentials", "用户名或密码错误。")
        return authenticated

    def set_password(
        self,
        user_id: str,
        password: str,
        *,
        must_change_password: bool = False,
    ) -> None:
        password_hash = hash_password(password)
        with _auth_db() as conn:
            cursor = conn.execute(
                """UPDATE auth_users SET password_hash=?,must_change_password=?,
                          failed_attempts=0,locked_until=NULL,updated_at=?
                   WHERE id=?""",
                (password_hash, int(must_change_password), _iso(), user_id),
            )
            if cursor.rowcount != 1:
                raise AuthError("user_not_found", "用户不存在。")
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            conn.execute(
                """UPDATE auth_api_tokens SET revoked_at=?
                   WHERE user_id=? AND revoked_at IS NULL""",
                (_iso(), user_id),
            )

    def set_active(self, user_id: str, active: bool) -> dict[str, Any]:
        initialize_auth()
        with _auth_db() as conn:
            row = conn.execute(
                "SELECT is_admin,is_active FROM auth_users WHERE id=?", (user_id,)
            ).fetchone()
            if not row:
                raise AuthError("user_not_found", "用户不存在。")
            if row["is_admin"] and row["is_active"] and not active:
                active_admins = int(conn.execute(
                    """SELECT COUNT(*) FROM auth_users
                       WHERE is_admin=1 AND is_active=1"""
                ).fetchone()[0])
                if active_admins <= 1:
                    raise AuthError("last_admin", "不能停用最后一个管理员。")
            conn.execute(
                "UPDATE auth_users SET is_active=?,updated_at=? WHERE id=?",
                (int(active), _iso(), user_id),
            )
            if not active:
                conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
                conn.execute(
                    """UPDATE auth_api_tokens SET revoked_at=?
                       WHERE user_id=? AND revoked_at IS NULL""",
                    (_iso(), user_id),
                )
        return self.get_user(user_id)

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> None:
        validate_password(new_password)
        with _auth_db() as conn:
            row = conn.execute(
                "SELECT password_hash FROM auth_users WHERE id=? AND is_active=1",
                (user_id,),
            ).fetchone()
        if not row or not verify_password(current_password, row["password_hash"]):
            raise AuthError("invalid_password", "当前密码错误。")
        if hmac.compare_digest(current_password, new_password):
            raise AuthError("password_unchanged", "新密码不能与当前密码相同。")
        self.set_password(user_id, new_password, must_change_password=False)

    def create_session(
        self,
        user_id: str,
        *,
        session_hours: int,
        ip_address: str = "",
        user_agent: str = "",
    ) -> tuple[str, str, str]:
        initialize_auth()
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        now = _now_dt()
        expires_at = now + timedelta(hours=max(1, min(int(session_hours), 720)))
        with _auth_db() as conn:
            conn.execute(
                """INSERT INTO auth_sessions(
                       token_hash,user_id,csrf_hash,created_at,expires_at,
                       last_seen_at,ip_address,user_agent)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    _token_hash(token),
                    user_id,
                    _token_hash(csrf_token),
                    _iso(now),
                    _iso(expires_at),
                    _iso(now),
                    str(ip_address or "")[:255],
                    str(user_agent or "")[:1000],
                ),
            )
            conn.execute(
                "DELETE FROM auth_sessions WHERE expires_at<=? OR revoked_at IS NOT NULL",
                (_iso(now),),
            )
        return token, csrf_token, _iso(expires_at)

    def resolve_session(self, token: str) -> Optional[dict[str, Any]]:
        if not token:
            return None
        initialize_auth()
        now = _now_dt()
        with _auth_db() as conn:
            row = conn.execute(
                """SELECT u.*,s.token_hash AS session_hash,
                          s.csrf_hash,s.expires_at,s.last_seen_at
                   FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.revoked_at IS NULL
                     AND s.expires_at>? AND u.is_active=1""",
                (_token_hash(token), _iso(now)),
            ).fetchone()
            if not row:
                return None
            last_seen = _parse_time(row["last_seen_at"])
            if not last_seen or now - last_seen >= timedelta(minutes=5):
                conn.execute(
                    "UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=?",
                    (_iso(now), row["session_hash"]),
                )
        principal = _public_user(row)
        principal.update({
            "auth_method": "session",
            "session_hash": row["session_hash"],
            "csrf_hash": row["csrf_hash"],
            "session_expires_at": row["expires_at"],
        })
        return principal

    def verify_csrf(self, principal: dict[str, Any], csrf_token: str) -> bool:
        expected = str(principal.get("csrf_hash") or "")
        return bool(expected and csrf_token) and hmac.compare_digest(
            expected, _token_hash(csrf_token)
        )

    def revoke_session_hash(self, session_hash: str) -> None:
        if not session_hash:
            return
        with _auth_db() as conn:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE token_hash=?",
                (_iso(), session_hash),
            )

    def create_api_token(
        self,
        username: str,
        name: str,
        *,
        expires_days: Optional[int] = None,
    ) -> tuple[str, dict[str, Any]]:
        user = self.get_user_by_username(username)
        if not user["is_active"]:
            raise AuthError("user_inactive", "用户已停用。")
        token = "arm_" + secrets.token_urlsafe(40)
        token_hash = _token_hash(token)
        now = _now_dt()
        expires_at = (
            _iso(now + timedelta(days=max(1, int(expires_days))))
            if expires_days is not None
            else None
        )
        token_name = str(name or "").strip()
        if not token_name or len(token_name) > 100:
            raise AuthError("invalid_token_name", "Token 名称不能为空且不超过 100 个字符。")
        with _auth_db() as conn:
            conn.execute(
                """INSERT INTO auth_api_tokens(
                       token_hash,user_id,name,created_at,expires_at)
                   VALUES (?,?,?,?,?)""",
                (token_hash, user["id"], token_name, _iso(now), expires_at),
            )
        return token, {
            "id": token_hash,
            "name": token_name,
            "username": user["username"],
            "created_at": _iso(now),
            "expires_at": expires_at,
        }

    def resolve_api_token(self, token: str) -> Optional[dict[str, Any]]:
        if not token or not token.startswith("arm_"):
            return None
        initialize_auth()
        now = _iso()
        with _auth_db() as conn:
            row = conn.execute(
                """SELECT u.*,t.token_hash AS api_token_hash,t.name AS token_name
                   FROM auth_api_tokens t JOIN auth_users u ON u.id=t.user_id
                   WHERE t.token_hash=? AND t.revoked_at IS NULL
                     AND (t.expires_at IS NULL OR t.expires_at>?)
                     AND u.is_active=1""",
                (_token_hash(token), now),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE auth_api_tokens SET last_used_at=? WHERE token_hash=?",
                (now, row["api_token_hash"]),
            )
        principal = _public_user(row)
        principal.update({
            "auth_method": "bearer",
            "token_name": row["token_name"],
            "api_token_hash": row["api_token_hash"],
        })
        return principal

    def list_api_tokens(self, user_id: str) -> list[dict[str, Any]]:
        initialize_auth()
        with _auth_db() as conn:
            rows = conn.execute(
                """SELECT token_hash,name,created_at,expires_at,last_used_at
                   FROM auth_api_tokens WHERE user_id=? AND revoked_at IS NULL
                   ORDER BY created_at DESC""",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": row["token_hash"],
                "name": row["name"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "last_used_at": row["last_used_at"],
            }
            for row in rows
        ]

    def revoke_api_token(self, user_id: str, token_hash: str) -> bool:
        initialize_auth()
        with _auth_db() as conn:
            cursor = conn.execute(
                """UPDATE auth_api_tokens SET revoked_at=?
                   WHERE token_hash=? AND user_id=? AND revoked_at IS NULL""",
                (_iso(), token_hash, user_id),
            )
        return cursor.rowcount == 1


auth_repository = AuthRepository()
