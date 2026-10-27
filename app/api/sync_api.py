"""Sync control API.

All ``POST`` endpoints are non-blocking: they dispatch the sync job to the
SyncManager (background thread) and return immediately. The frontend polls
``GET /api/sync/status`` to follow progress.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.scheduler import trigger_sync_now, get_sync_status, sync_manager
from app.database import get_db, rebuild_fts
from app.config import reload_config
from app.sync.device_store import (
    SyncDeviceError,
    sync_device_repository,
)

router = APIRouter(prefix="/api/sync", tags=["sync"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSyncDeviceRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(default="", max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=2048)
    password_env: str = Field(default="", max_length=255)
    copilot_dir: str = Field(default="~/.copilot/session-state", max_length=2048)
    codex_dir: str = Field(default="~/.codex", max_length=2048)
    claude_dir: str = Field(default="~/.claude", max_length=2048)
    claude_projects_dir: str = Field(default="", max_length=2048)
    opencode_dir: str = Field(default="~/.local/share/opencode", max_length=2048)
    vscode_dir: str = Field(default="~/.vscode-server", max_length=2048)
    enabled: bool = True
    is_local: bool = False
    is_mac: bool = False

    @model_validator(mode="after")
    def validate_device_type(self):
        if self.is_local and self.is_mac:
            raise ValueError("本机设备不能同时标记为 Mac 远端设备")
        if not self.is_local and not self.host.strip():
            raise ValueError("远端设备必须填写主机地址")
        return self


class UpdateSyncDeviceRequest(_StrictModel):
    host: Optional[str] = Field(default=None, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    user: Optional[str] = Field(default=None, max_length=255)
    password: Optional[str] = Field(default=None, max_length=2048)
    password_env: Optional[str] = Field(default=None, max_length=255)
    clear_password: bool = False
    copilot_dir: Optional[str] = Field(default=None, max_length=2048)
    codex_dir: Optional[str] = Field(default=None, max_length=2048)
    claude_dir: Optional[str] = Field(default=None, max_length=2048)
    claude_projects_dir: Optional[str] = Field(default=None, max_length=2048)
    opencode_dir: Optional[str] = Field(default=None, max_length=2048)
    vscode_dir: Optional[str] = Field(default=None, max_length=2048)
    enabled: Optional[bool] = None
    is_local: Optional[bool] = None
    is_mac: Optional[bool] = None


def _device_error(exc: SyncDeviceError) -> HTTPException:
    status = (
        404
        if str(exc) == "设备不存在"
        else 409
        if "已存在" in str(exc)
        else 400
    )
    return HTTPException(status_code=status, detail=str(exc))


def _session_counts_by_device() -> dict[str, int]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT COALESCE(server_name,'local') AS name,COUNT(*) AS count "
            "FROM sessions GROUP BY COALESCE(server_name,'local')"
        ).fetchall()
    return {row["name"]: int(row["count"]) for row in rows}


def _list_devices_with_counts() -> list[dict]:
    counts = _session_counts_by_device()
    items = sync_device_repository.list()
    for item in items:
        retained_name = "local" if item["is_local"] else item["name"]
        item["retained_session_count"] = counts.get(retained_name, 0)
    return items


@router.post("/now")
def trigger_sync():
    """Trigger immediate sync of all servers (non-blocking)."""
    return trigger_sync_now(mode="all", force=False)


@router.post("/local")
def trigger_local_sync(force: bool = Query(False)):
    """Sync only local sessions (non-blocking)."""
    return trigger_sync_now(mode="local", force=force)


@router.post("/force-reimport")
def force_reimport():
    """Force re-import ALL sessions (local + remote) using updated parsers.

    Non-blocking — returns immediately while the work proceeds in a
    background thread. Poll ``GET /api/sync/status`` for progress.
    """
    return trigger_sync_now(mode="force-reimport", force=True)


@router.get("/status")
async def sync_status():
    """Current SyncManager state for the frontend poll loop."""
    return get_sync_status()


@router.post("/wait")
def sync_wait(timeout: float = Query(30.0, ge=0.0, le=600.0)):
    """Block up to ``timeout`` seconds waiting for the current job to finish.

    Used by tests and CLI tools that want to await completion without
    busy-polling the status endpoint. The frontend should poll /status.
    """
    idle = sync_manager.wait(timeout=timeout)
    return {"idle": idle, **get_sync_status()}


@router.post("/rebuild-fts")
def api_rebuild_fts():
    """Force rebuild FTS indexes (trigram)."""
    rebuild_fts()
    return {"status": "ok", "message": "FTS indexes rebuilt with trigram tokenizer"}


@router.get("/log")
def sync_log(limit: int = 20):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_log ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"logs": [dict(r) for r in rows]}


@router.get("/servers")
def list_servers():
    """Compatibility alias for clients predating device management."""
    return {"servers": _list_devices_with_counts()}


@router.get("/devices")
def list_devices():
    items = _list_devices_with_counts()
    return {"items": items, "total": len(items)}


@router.post("/devices", status_code=201)
def create_device(payload: CreateSyncDeviceRequest):
    try:
        return sync_device_repository.create(payload.model_dump())
    except SyncDeviceError as exc:
        raise _device_error(exc) from exc


@router.patch("/devices/{device_id}")
def update_device(device_id: str, payload: UpdateSyncDeviceRequest):
    values = payload.model_dump(exclude_unset=True)
    clear_password = bool(values.pop("clear_password", False))
    if values.get("password") == "":
        # Blank in the editor means keep the write-only saved password.
        values.pop("password")
    if clear_password:
        values["password"] = ""
    try:
        return sync_device_repository.update(device_id, values)
    except SyncDeviceError as exc:
        raise _device_error(exc) from exc


@router.delete("/devices/{device_id}")
def delete_device(device_id: str):
    try:
        device = sync_device_repository.get(device_id)
        retained_name = "local" if device["is_local"] else device["name"]
        with get_db() as conn:
            retained = int(conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE COALESCE(server_name,'local')=?",
                (retained_name,),
            ).fetchone()[0])
        deleted = sync_device_repository.delete(device_id)
    except SyncDeviceError as exc:
        raise _device_error(exc) from exc
    return {
        "deleted": True,
        "device": deleted,
        "retained_sessions": retained,
        "message": "设备连接已删除，历史会话与整理数据已保留。",
    }


@router.post("/devices/{device_id}/test")
def test_device(device_id: str):
    try:
        device = sync_device_repository.get_config(device_id)
    except SyncDeviceError as exc:
        raise _device_error(exc) from exc
    if device.is_local:
        return {"available": True, "detail": "本机目录将直接读取，无需 SSH。"}
    from app.sync.ssh_sync import SSHSyncer
    syncer = SSHSyncer(device)
    try:
        syncer.connect()
        return {"available": True, "detail": f"已连接 {device.host}:{device.port}"}
    except Exception as exc:  # Paramiko exposes several transport error types
        return {"available": False, "detail": str(exc)[-1000:]}
    finally:
        syncer.disconnect()


@router.post("/reload-config")
def api_reload_config():
    reload_config()
    from app.enrichment import enrichment_manager
    enrichment_manager.invalidate_provider_status()
    return {
        "status": "ok",
        "servers": len(sync_device_repository.list()),
        "note": "设备由持久化设备库管理，未被 YAML 重载覆盖。",
    }
