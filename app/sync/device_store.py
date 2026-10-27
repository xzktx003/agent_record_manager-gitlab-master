"""Persistent sync-device configuration.

The original deployment read devices only from ``config.yaml``.  That made
runtime add/remove impossible and encouraged writing SSH passwords back to a
world-readable configuration file.  Devices now live in a small, private
control-plane database.  Existing YAML entries are imported exactly once.

Deleting a device only deletes its connection profile.  Conversation rows use
``sessions.server_name`` rather than a foreign key to this database, so all
previously synchronized data remains available.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from app.config import ServerConfig, get_config
from app.database import get_db_path
from app.tenant import LEGACY_DATA_SCOPE, get_data_scope


_DEVICE_DB_BUSY_TIMEOUT_MS = 5000
_schema_lock = threading.Lock()
_schema_path: Optional[str] = None


class SyncDeviceError(ValueError):
    """A user-correctable device configuration error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_sync_device_db_path() -> str:
    configured = os.environ.get("ARM_SYNC_DEVICE_DB", "").strip()
    if configured:
        configured_path = os.path.abspath(os.path.expanduser(configured))
        scope = get_data_scope()
        if scope == LEGACY_DATA_SCOPE:
            return configured_path
        return os.path.join(
            os.path.dirname(configured_path),
            "users",
            scope,
            os.path.basename(configured_path),
        )
    main_path = os.path.abspath(get_db_path())
    stem, extension = os.path.splitext(main_path)
    return f"{stem}.devices{extension or '.db'}"


def _secure_device_db_files() -> None:
    path = get_sync_device_db_path()
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:
            # SQLite creates and removes WAL sidecars concurrently with readers.
            # Existence checks introduce a TOCTOU race, so chmod directly.
            continue


@contextmanager
def _device_db():
    path = get_sync_device_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=_DEVICE_DB_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_DEVICE_DB_BUSY_TIMEOUT_MS}")
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
        _secure_device_db_files()


def _seed_values(server: ServerConfig) -> dict[str, Any]:
    values = asdict(server)
    # Environment-backed credentials should remain environment-backed; do not
    # persist the resolved secret copied into ServerConfig by load_config().
    if values.get("password_env"):
        values["password"] = ""
    return values


def _default_local_seed(name: str) -> ServerConfig:
    """Build the initial local profile from the existing local settings."""
    local = get_config().local
    return ServerConfig(
        name=name,
        host="127.0.0.1",
        copilot_dir=local.copilot_dir,
        codex_dir=local.codex_dir,
        claude_dir=local.claude_dir,
        claude_projects_dir=local.claude_projects_dir,
        opencode_dir=local.opencode_dir,
        vscode_dir=local.vscode_dir,
        is_local=True,
    )


def _insert_device(conn: sqlite3.Connection, values: dict[str, Any]) -> str:
    device_id = str(values.get("id") or uuid.uuid4())
    conn.execute(
        """INSERT INTO sync_devices(
               id,name,host,port,user,password,password_env,copilot_dir,codex_dir,
               claude_dir,claude_projects_dir,opencode_dir,vscode_dir,enabled,
               is_local,is_mac,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            device_id,
            values["name"],
            values.get("host") or "",
            int(values.get("port") or 22),
            values.get("user") or "",
            values.get("password") or "",
            values.get("password_env") or "",
            values.get("copilot_dir") or "~/.copilot/session-state",
            values.get("codex_dir") or "~/.codex",
            values.get("claude_dir") or "~/.claude",
            values.get("claude_projects_dir") or "",
            (
                values.get("opencode_dir", "~/.local/share/opencode")
                if values.get("is_local", False)
                else values.get("opencode_dir") or "~/.local/share/opencode"
            ),
            values.get("vscode_dir") or "~/.vscode-server",
            int(bool(values.get("enabled", True))),
            int(bool(values.get("is_local", False))),
            int(bool(values.get("is_mac", False))),
            values.get("created_at") or _now(),
            values.get("updated_at") or _now(),
        ),
    )
    return device_id


def initialize_sync_devices() -> None:
    """Create the device store and import YAML devices once per database."""
    global _schema_path
    path = get_sync_device_db_path()
    if _schema_path == path:
        return
    with _schema_lock:
        if _schema_path == path:
            return
        with _device_db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sync_devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    host TEXT NOT NULL DEFAULT '',
                    port INTEGER NOT NULL DEFAULT 22,
                    user TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    password_env TEXT NOT NULL DEFAULT '',
                    copilot_dir TEXT NOT NULL DEFAULT '~/.copilot/session-state',
                    codex_dir TEXT NOT NULL DEFAULT '~/.codex',
                    claude_dir TEXT NOT NULL DEFAULT '~/.claude',
                    claude_projects_dir TEXT NOT NULL DEFAULT '',
                    opencode_dir TEXT NOT NULL DEFAULT '~/.local/share/opencode',
                    vscode_dir TEXT NOT NULL DEFAULT '~/.vscode-server',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    is_local INTEGER NOT NULL DEFAULT 0,
                    is_mac INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_device_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            seeded = conn.execute(
                "SELECT value FROM sync_device_meta WHERE key='yaml_seeded'"
            ).fetchone()
            if not seeded:
                legacy_scope = get_data_scope() == LEGACY_DATA_SCOPE
                configured_servers = (
                    list(get_config().servers) if legacy_scope else []
                )
                for server in configured_servers:
                    try:
                        _insert_device(conn, _seed_values(server))
                    except sqlite3.IntegrityError:
                        # A partially completed prior import can safely keep the
                        # already-created profile with the same name.
                        continue
                if legacy_scope and not any(
                    server.is_local for server in configured_servers
                ):
                    local_name = "Local"
                    suffix = 2
                    while conn.execute(
                        "SELECT 1 FROM sync_devices WHERE name=?", (local_name,)
                    ).fetchone():
                        local_name = f"Local ({suffix})"
                        suffix += 1
                    _insert_device(
                        conn,
                        _seed_values(_default_local_seed(local_name)),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO sync_device_meta(key,value) VALUES ('yaml_seeded',?)",
                    (_now(),),
                )
        _schema_path = path
    _secure_device_db_files()


def _validate_values(values: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    result = dict(values)
    if creating or "name" in result:
        name = str(result.get("name") or "").strip()
        if not name:
            raise SyncDeviceError("设备名称不能为空")
        if len(name) > 100:
            raise SyncDeviceError("设备名称不能超过 100 个字符")
        result["name"] = name
    if creating or "host" in result:
        result["host"] = str(result.get("host") or "").strip()
    if "user" in result:
        result["user"] = str(result.get("user") or "").strip()
    if "password_env" in result:
        result["password_env"] = str(result.get("password_env") or "").strip()
    if "port" in result:
        port = int(result.get("port") or 22)
        if not 1 <= port <= 65535:
            raise SyncDeviceError("SSH 端口必须在 1 到 65535 之间")
        result["port"] = port
    if bool(result.get("is_local")) and bool(result.get("is_mac")):
        raise SyncDeviceError("本机设备不能同时标记为 Mac 远端设备")
    if (
        (creating or "is_local" in result)
        and not result.get("is_local")
        and not result.get("host")
    ):
        raise SyncDeviceError("远端设备必须填写主机地址")
    return result


def _row_to_public(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    password_env = value.pop("password_env", "") or ""
    password = value.pop("password", "") or ""
    value["enabled"] = bool(value["enabled"])
    value["is_local"] = bool(value["is_local"])
    value["is_mac"] = bool(value["is_mac"])
    value["credential_configured"] = bool(password or password_env)
    value["credential_available"] = bool(
        password or (password_env and os.environ.get(password_env))
    )
    value["credential_source"] = (
        "environment" if password_env else ("saved" if password else "none")
    )
    value["password_env"] = password_env
    value["device_type"] = (
        "local" if value["is_local"] else ("mac" if value["is_mac"] else "linux")
    )
    return value


def _row_to_server_config(row: sqlite3.Row) -> ServerConfig:
    value = dict(row)
    password_env = value.get("password_env") or ""
    password = (
        os.environ.get(password_env, value.get("password", ""))
        if password_env
        else value.get("password", "")
    )
    return ServerConfig(
        name=value["name"],
        host=value["host"],
        port=int(value["port"]),
        user=value["user"],
        password=password or "",
        password_env=password_env,
        copilot_dir=value["copilot_dir"],
        codex_dir=value["codex_dir"],
        claude_dir=value["claude_dir"],
        claude_projects_dir=value["claude_projects_dir"],
        opencode_dir=value["opencode_dir"],
        vscode_dir=value["vscode_dir"],
        enabled=bool(value["enabled"]),
        is_local=bool(value["is_local"]),
        is_mac=bool(value["is_mac"]),
    )


class SyncDeviceRepository:
    _UPDATABLE_FIELDS = {
        "host", "port", "user", "password", "password_env", "copilot_dir",
        "codex_dir", "claude_dir", "claude_projects_dir", "opencode_dir",
        "vscode_dir", "enabled", "is_local", "is_mac",
    }

    def list(self) -> list[dict[str, Any]]:
        initialize_sync_devices()
        with _device_db() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_devices ORDER BY is_local DESC,name COLLATE NOCASE"
            ).fetchall()
        return [_row_to_public(row) for row in rows]

    def list_configs(self, *, enabled_only: bool = False) -> list[ServerConfig]:
        initialize_sync_devices()
        query = "SELECT * FROM sync_devices"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY is_local DESC,name COLLATE NOCASE"
        with _device_db() as conn:
            rows = conn.execute(query).fetchall()
        return [_row_to_server_config(row) for row in rows]

    def get(self, device_id: str) -> dict[str, Any]:
        initialize_sync_devices()
        with _device_db() as conn:
            row = conn.execute(
                "SELECT * FROM sync_devices WHERE id=?", (device_id,)
            ).fetchone()
        if not row:
            raise SyncDeviceError("设备不存在")
        return _row_to_public(row)

    def get_config(self, device_id: str) -> ServerConfig:
        initialize_sync_devices()
        with _device_db() as conn:
            row = conn.execute(
                "SELECT * FROM sync_devices WHERE id=?", (device_id,)
            ).fetchone()
        if not row:
            raise SyncDeviceError("设备不存在")
        return _row_to_server_config(row)

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        initialize_sync_devices()
        values = _validate_values(values, creating=True)
        if values.get("is_local") and get_data_scope() != LEGACY_DATA_SCOPE:
            raise SyncDeviceError("只有初始管理员数据域可以读取服务主机的本机目录")
        try:
            with _device_db() as conn:
                device_id = _insert_device(conn, values)
        except sqlite3.IntegrityError as exc:
            raise SyncDeviceError("设备名称已存在") from exc
        return self.get(device_id)

    def update(self, device_id: str, values: dict[str, Any]) -> dict[str, Any]:
        initialize_sync_devices()
        unknown = set(values) - self._UPDATABLE_FIELDS
        if unknown:
            raise SyncDeviceError(f"不支持修改字段：{', '.join(sorted(unknown))}")
        if not values:
            return self.get(device_id)
        current = self.get(device_id)
        merged = dict(current)
        merged.update(values)
        _validate_values(merged, creating=False)
        if merged.get("is_local") and get_data_scope() != LEGACY_DATA_SCOPE:
            raise SyncDeviceError("只有初始管理员数据域可以读取服务主机的本机目录")
        values = _validate_values(values, creating=False)
        assignments = []
        params: list[Any] = []
        for field, value in values.items():
            assignments.append(f"{field}=?")
            params.append(int(value) if field in {"enabled", "is_local", "is_mac"} else value)
        assignments.append("updated_at=?")
        params.extend([_now(), device_id])
        with _device_db() as conn:
            cursor = conn.execute(
                f"UPDATE sync_devices SET {','.join(assignments)} WHERE id=?",
                params,
            )
            if cursor.rowcount != 1:
                raise SyncDeviceError("设备不存在")
        return self.get(device_id)

    def delete(self, device_id: str) -> dict[str, Any]:
        initialize_sync_devices()
        with _device_db() as conn:
            row = conn.execute(
                "SELECT * FROM sync_devices WHERE id=?", (device_id,)
            ).fetchone()
            if not row:
                raise SyncDeviceError("设备不存在")
            conn.execute("DELETE FROM sync_devices WHERE id=?", (device_id,))
        return _row_to_public(row)


sync_device_repository = SyncDeviceRepository()


def get_sync_server_configs(*, enabled_only: bool = False) -> Iterable[ServerConfig]:
    """Return the current persisted device snapshot for a sync run."""
    return sync_device_repository.list_configs(enabled_only=enabled_only)
