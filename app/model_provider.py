"""Persistent model-provider profiles, secrets, diagnostics, and selection."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.config import get_config
from app.database import get_db, get_db_path
from app.tenant import LEGACY_DATA_SCOPE, get_data_scope


ProviderKind = Literal["opencode_cli", "codex_cli", "openai_compatible"]
ReasoningEffort = Literal["inherit", "low", "medium", "high", "xhigh", "max", "ultra"]
BUILTIN_CODEX_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "agent-record-manager:codex-cli-default")
)
_PROVIDER_DB_BUSY_TIMEOUT_MS = 5000
_PROVIDER_COLUMNS = (
    "id", "name", "kind", "model", "base_url", "credential_ref", "executable",
    "reasoning_effort", "use_proxy", "timeout_seconds", "enabled", "is_default",
    "created_at", "updated_at",
)


class ModelProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelProviderProfile(_StrictModel):
    id: str
    name: str
    kind: ProviderKind
    model: str = ""
    base_url: Optional[str] = None
    credential_ref: Optional[str] = Field(default=None, exclude=True, repr=False)
    credential_configured: bool = False
    credential_source: Literal["none", "saved", "environment"] = "none"
    managed: bool = False
    executable: str = ""
    reasoning_effort: ReasoningEffort = "inherit"
    use_proxy: bool = False
    timeout_seconds: int = 600
    enabled: bool = True
    is_default: bool = False
    last_tested_at: Optional[str] = None
    last_test_ok: Optional[bool] = None
    last_test_detail: Optional[str] = None
    last_test_latency_ms: Optional[int] = None
    created_at: str
    updated_at: str


class CreateModelProviderRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: ProviderKind
    model: str = Field(default="", max_length=300)
    base_url: Optional[str] = Field(default=None, max_length=2000)
    credential_ref: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$", max_length=100
    )
    api_key: Optional[SecretStr] = Field(default=None, max_length=8000)
    executable: str = Field(default="", max_length=500)
    reasoning_effort: ReasoningEffort = "medium"
    use_proxy: bool = False
    timeout_seconds: int = Field(default=600, ge=10, le=1800)
    enabled: bool = True
    is_default: bool = False

    @field_validator("name", "model", "executable")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must be an HTTP(S) URL without credentials, query, or fragment"
            )
        return normalized

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "CreateModelProviderRequest":
        if self.kind == "openai_compatible":
            if not self.base_url or not self.model:
                raise ValueError("openai_compatible requires base_url and model")
            if self.reasoning_effort == "inherit":
                raise ValueError("openai_compatible requires an explicit reasoning effort")
        elif not self.executable:
            raise ValueError("CLI providers require executable")
        if self.kind != "codex_cli" and self.reasoning_effort == "inherit":
            raise ValueError("only codex_cli can inherit reasoning effort")
        if self.api_key is not None and self.credential_ref is not None:
            raise ValueError("api_key and credential_ref are mutually exclusive")
        if self.is_default and not self.enabled:
            raise ValueError("the default provider must be enabled")
        return self


class UpdateModelProviderRequest(_StrictModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    model: Optional[str] = Field(default=None, max_length=300)
    base_url: Optional[str] = Field(default=None, max_length=2000)
    credential_ref: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$", max_length=100
    )
    api_key: Optional[SecretStr] = Field(default=None, max_length=8000)
    clear_api_key: bool = False
    executable: Optional[str] = Field(default=None, max_length=500)
    reasoning_effort: Optional[ReasoningEffort] = None
    use_proxy: Optional[bool] = None
    timeout_seconds: Optional[int] = Field(default=None, ge=10, le=1800)
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None

    @field_validator("name", "model", "executable")
    @classmethod
    def normalize_optional_text(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: Optional[str]) -> Optional[str]:
        return CreateModelProviderRequest.validate_base_url(value)


def get_model_provider_db_path() -> str:
    """Keep small control-plane writes independent from large session imports."""
    configured = os.environ.get("ARM_MODEL_PROVIDER_DB", "").strip()
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
    return f"{stem}.providers{extension or '.db'}"


def _secure_provider_db_files() -> None:
    path = get_model_provider_db_path()
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:
            continue


@contextmanager
def _provider_db():
    path = get_model_provider_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=_PROVIDER_DB_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_PROVIDER_DB_BUSY_TIMEOUT_MS}")
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
        _secure_provider_db_files()


class ModelProviderRepository:
    def __init__(self) -> None:
        self._schema_paths: set[str] = set()
        self._schema_lock = Lock()

    def ensure_schema(self) -> None:
        path = get_model_provider_db_path()
        if path in self._schema_paths:
            return
        with self._schema_lock:
            if path in self._schema_paths:
                return
            with _provider_db() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS model_providers (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL,
                        model TEXT NOT NULL DEFAULT '',
                        base_url TEXT,
                        credential_ref TEXT,
                        executable TEXT NOT NULL DEFAULT '',
                        reasoning_effort TEXT NOT NULL DEFAULT 'medium',
                        use_proxy INTEGER NOT NULL DEFAULT 0,
                        timeout_seconds INTEGER NOT NULL DEFAULT 600,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        is_default INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_model_providers_one_default
                        ON model_providers(is_default) WHERE is_default=1;
                    CREATE TABLE IF NOT EXISTS model_provider_secrets (
                        provider_id TEXT PRIMARY KEY
                            REFERENCES model_providers(id) ON DELETE CASCADE,
                        secret_value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS model_provider_health (
                        provider_id TEXT PRIMARY KEY
                            REFERENCES model_providers(id) ON DELETE CASCADE,
                        diagnostic_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS model_provider_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(model_providers)"
                    ).fetchall()
                }
                if "use_proxy" not in columns:
                    conn.execute(
                        "ALTER TABLE model_providers ADD COLUMN "
                        "use_proxy INTEGER NOT NULL DEFAULT 0"
                    )
                    # Existing Codex profiles previously depended on whatever
                    # proxy environment the service happened to inherit.
                    conn.execute(
                        "UPDATE model_providers SET use_proxy=1 "
                        "WHERE kind='codex_cli'"
                    )
            self._schema_paths.add(path)

    def _migrate_legacy_profiles_once(self) -> None:
        """Copy legacy rows once, leaving the original database as a backup."""
        with _provider_db() as conn:
            migrated = conn.execute(
                "SELECT 1 FROM model_provider_meta WHERE key='legacy_main_db_migrated'"
            ).fetchone()
        if migrated:
            return

        providers: list[dict] = []
        secrets: list[dict] = []
        health: list[dict] = []
        with get_db() as source:
            tables = {
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "model_providers" in tables:
                providers = [
                    dict(row)
                    for row in source.execute("SELECT * FROM model_providers").fetchall()
                ]
            if "model_provider_secrets" in tables:
                secrets = [
                    dict(row)
                    for row in source.execute("SELECT * FROM model_provider_secrets").fetchall()
                ]
            if "model_provider_health" in tables:
                health = [
                    dict(row)
                    for row in source.execute("SELECT * FROM model_provider_health").fetchall()
                ]

        placeholders = ",".join("?" for _ in _PROVIDER_COLUMNS)
        with _provider_db() as target:
            for row in providers:
                target.execute(
                    f"INSERT OR IGNORE INTO model_providers({','.join(_PROVIDER_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    tuple(
                        int(row.get("kind") == "codex_cli")
                        if column == "use_proxy" and column not in row
                        else row.get(column)
                        for column in _PROVIDER_COLUMNS
                    ),
                )
            for row in secrets:
                target.execute(
                    """INSERT OR IGNORE INTO model_provider_secrets(
                           provider_id,secret_value,updated_at) VALUES (?,?,?)""",
                    (row["provider_id"], row["secret_value"], row["updated_at"]),
                )
            for row in health:
                target.execute(
                    """INSERT OR IGNORE INTO model_provider_health(
                           provider_id,diagnostic_json,updated_at) VALUES (?,?,?)""",
                    (row["provider_id"], row["diagnostic_json"], row["updated_at"]),
                )
            target.execute(
                """INSERT INTO model_provider_meta(key,value)
                   VALUES ('legacy_main_db_migrated',?)""",
                (_now(),),
            )

    def ensure_builtin_codex(self) -> None:
        self.ensure_schema()
        self._migrate_legacy_profiles_once()
        with _provider_db() as conn:
            codex = conn.execute(
                "SELECT is_default FROM model_providers WHERE id=?",
                (BUILTIN_CODEX_ID,),
            ).fetchone()
            has_default = bool(
                conn.execute(
                    "SELECT 1 FROM model_providers WHERE is_default=1 LIMIT 1"
                ).fetchone()
            )
            now = _now()
            if not codex:
                conn.execute(
                    """INSERT INTO model_providers(
                           id,name,kind,model,base_url,credential_ref,executable,
                           reasoning_effort,use_proxy,timeout_seconds,enabled,is_default,
                           created_at,updated_at)
                       VALUES (?,?,?,'',NULL,NULL,?,'inherit',1,600,1,0,?,?)""",
                    (
                        BUILTIN_CODEX_ID,
                        "Codex CLI",
                        "codex_cli",
                        "codex",
                        now,
                        now,
                    ),
                )
            if not has_default:
                conn.execute(
                    "UPDATE model_providers SET is_default=1 WHERE id=?",
                    (BUILTIN_CODEX_ID,),
                )

    def list(self) -> list[dict]:
        self.ensure_schema()
        with _provider_db() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM model_providers
                       ORDER BY is_default DESC, enabled DESC, name COLLATE NOCASE"""
                ).fetchall()
            ]

    def get(self, provider_id: str) -> dict:
        self.ensure_schema()
        with _provider_db() as conn:
            row = conn.execute(
                "SELECT * FROM model_providers WHERE id=?", (provider_id,)
            ).fetchone()
        if not row:
            raise ModelProviderError("provider_not_found", "模型 Provider 不存在。")
        return dict(row)

    def default(self) -> dict:
        self.ensure_schema()
        with _provider_db() as conn:
            row = conn.execute(
                """SELECT * FROM model_providers WHERE is_default=1
                   ORDER BY updated_at DESC LIMIT 1"""
            ).fetchone()
        if not row:
            raise ModelProviderError("provider_not_found", "尚未设置默认模型 Provider。")
        return dict(row)

    def create(self, values: dict, secret: Optional[str]) -> dict:
        self.ensure_schema()
        provider_id = str(uuid.uuid4())
        now = _now()
        with _provider_db() as conn:
            try:
                if values.get("is_default"):
                    conn.execute("UPDATE model_providers SET is_default=0")
                conn.execute(
                    """INSERT INTO model_providers(
                           id,name,kind,model,base_url,credential_ref,executable,
                           reasoning_effort,use_proxy,timeout_seconds,enabled,is_default,
                           created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        provider_id,
                        values["name"],
                        values["kind"],
                        values.get("model") or "",
                        values.get("base_url"),
                        values.get("credential_ref"),
                        values.get("executable") or "",
                        values.get("reasoning_effort") or "medium",
                        int(bool(values.get("use_proxy", False))),
                        int(values.get("timeout_seconds") or 600),
                        int(bool(values.get("enabled", True))),
                        int(bool(values.get("is_default", False))),
                        now,
                        now,
                    ),
                )
                if secret:
                    conn.execute(
                        """INSERT INTO model_provider_secrets(
                               provider_id,secret_value,updated_at) VALUES (?,?,?)""",
                        (provider_id, secret, now),
                    )
            except sqlite3.IntegrityError as exc:
                raise ModelProviderError("provider_exists", "Provider 名称已存在。") from exc
        return self.get(provider_id)

    def update(
        self,
        provider_id: str,
        changes: dict,
        secret: Optional[str],
        clear_secret: bool,
    ) -> dict:
        current = self.get(provider_id)
        self.ensure_schema()
        connection_fields = {
                "model",
                "base_url",
                "credential_ref",
                "executable",
                "reasoning_effort",
                "use_proxy",
                "timeout_seconds",
        }
        invalidate_health = any(
            key in connection_fields and changes[key] != current.get(key)
            for key in changes
        ) or bool(secret or clear_secret)
        with _provider_db() as conn:
            try:
                if changes.get("is_default") is True:
                    conn.execute("UPDATE model_providers SET is_default=0")
                if changes:
                    values = dict(changes)
                    values["updated_at"] = _now()
                    assignments = ",".join(f"{key}=?" for key in values)
                    conn.execute(
                        f"UPDATE model_providers SET {assignments} WHERE id=?",
                        (*values.values(), provider_id),
                    )
                if secret:
                    conn.execute(
                        """INSERT INTO model_provider_secrets(
                               provider_id,secret_value,updated_at) VALUES (?,?,?)
                           ON CONFLICT(provider_id) DO UPDATE SET
                               secret_value=excluded.secret_value,
                               updated_at=excluded.updated_at""",
                        (provider_id, secret, _now()),
                    )
                elif clear_secret:
                    conn.execute(
                        "DELETE FROM model_provider_secrets WHERE provider_id=?",
                        (provider_id,),
                    )
                if invalidate_health:
                    conn.execute(
                        "DELETE FROM model_provider_health WHERE provider_id=?",
                        (provider_id,),
                    )
            except sqlite3.IntegrityError as exc:
                raise ModelProviderError("provider_exists", "Provider 名称已存在。") from exc
        return self.get(provider_id)

    def delete(self, provider_id: str) -> None:
        current = self.get(provider_id)
        if provider_id == BUILTIN_CODEX_ID:
            raise ModelProviderError(
                "managed_provider", "Codex CLI 是系统内置 Provider，不能删除。"
            )
        if current["is_default"]:
            raise ModelProviderError(
                "default_provider_required", "默认 Provider 不能删除，请先切换默认模型。"
            )
        with _provider_db() as conn:
            conn.execute("DELETE FROM model_providers WHERE id=?", (provider_id,))

    def get_secret(self, provider_id: str) -> Optional[str]:
        self.ensure_schema()
        with _provider_db() as conn:
            row = conn.execute(
                "SELECT secret_value FROM model_provider_secrets WHERE provider_id=?",
                (provider_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def get_health(self, provider_id: str) -> dict:
        self.ensure_schema()
        with _provider_db() as conn:
            row = conn.execute(
                "SELECT diagnostic_json FROM model_provider_health WHERE provider_id=?",
                (provider_id,),
            ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def set_health(self, provider_id: str, diagnostic: dict) -> None:
        self.get(provider_id)
        now = _now()
        with _provider_db() as conn:
            conn.execute(
                """INSERT INTO model_provider_health(
                       provider_id,diagnostic_json,updated_at) VALUES (?,?,?)
                   ON CONFLICT(provider_id) DO UPDATE SET
                       diagnostic_json=excluded.diagnostic_json,
                       updated_at=excluded.updated_at""",
                (provider_id, json.dumps(diagnostic, ensure_ascii=False), now),
            )


class ModelProviderService:
    def __init__(self, repository: Optional[ModelProviderRepository] = None) -> None:
        self.repository = repository or ModelProviderRepository()
        self._initialized_paths: set[str] = set()
        self._initialize_lock = Lock()

    def initialize(self) -> None:
        path = get_model_provider_db_path()
        if path in self._initialized_paths:
            return
        with self._initialize_lock:
            if path in self._initialized_paths:
                return
            self.repository.ensure_builtin_codex()
            self._initialized_paths.add(path)

    def list(self) -> list[ModelProviderProfile]:
        self.initialize()
        return [self._decorate(row) for row in self.repository.list()]

    def get(self, provider_id: Optional[str] = None) -> ModelProviderProfile:
        self.initialize()
        row = self.repository.get(provider_id) if provider_id else self.repository.default()
        return self._decorate(row)

    def create(self, request: CreateModelProviderRequest) -> ModelProviderProfile:
        if request.kind == "codex_cli":
            raise ModelProviderError(
                "managed_provider", "Codex CLI 已自动接入，请直接编辑内置 Provider。"
            )
        values = request.model_dump(mode="python", exclude={"api_key"})
        secret = _secret_value(request.api_key)
        return self._decorate(self.repository.create(values, secret))

    def update(
        self, provider_id: str, request: UpdateModelProviderRequest
    ) -> ModelProviderProfile:
        current = self.get(provider_id)
        if request.api_key is not None and request.clear_api_key:
            raise ModelProviderError(
                "invalid_provider", "不能同时更新并清除 API Key。"
            )
        changes = request.model_dump(
            mode="python",
            exclude_unset=True,
            exclude={"api_key", "clear_api_key"},
        )
        if current.managed:
            unsupported = set(changes) - {
                "model", "reasoning_effort", "use_proxy", "timeout_seconds",
                "is_default",
            }
            if unsupported:
                raise ModelProviderError(
                    "managed_provider",
                    "内置 Codex CLI 只允许修改模型、思考强度、代理、超时和默认状态。",
                )
        if current.is_default and changes.get("is_default") is False:
            raise ModelProviderError(
                "default_provider_required", "请先把另一个 Provider 设为默认。"
            )
        if current.is_default and changes.get("enabled") is False:
            raise ModelProviderError(
                "default_provider_required", "默认 Provider 不能停用，请先切换默认模型。"
            )
        merged = {
            "name": current.name,
            "kind": current.kind,
            "model": current.model,
            "base_url": current.base_url,
            "credential_ref": current.credential_ref,
            "executable": current.executable,
            "reasoning_effort": current.reasoning_effort,
            "use_proxy": current.use_proxy,
            "timeout_seconds": current.timeout_seconds,
            "enabled": current.enabled,
            "is_default": current.is_default,
        }
        merged.update(changes)
        try:
            CreateModelProviderRequest.model_validate(merged)
        except ValueError as exc:
            raise ModelProviderError("invalid_provider", str(exc)) from exc
        secret = _secret_value(request.api_key)
        if secret:
            changes["credential_ref"] = None
        updated = self.repository.update(
            provider_id, changes, secret, request.clear_api_key
        )
        return self._decorate(updated)

    def delete(self, provider_id: str) -> None:
        self.repository.delete(provider_id)

    def credential(self, profile: ModelProviderProfile) -> Optional[str]:
        saved = self.repository.get_secret(profile.id)
        if saved:
            return saved
        return os.getenv(profile.credential_ref) if profile.credential_ref else None

    def build(self, profile: Optional[ModelProviderProfile] = None):
        profile = profile or self.get()
        if not profile.enabled:
            raise ModelProviderError("provider_disabled", "所选模型 Provider 已停用。")
        from app.agent_provider import (
            CodexCLIProvider,
            OpenAICompatibleProvider,
            OpenCodeCLIProvider,
        )

        if profile.kind == "codex_cli":
            return CodexCLIProvider(profile)
        if profile.kind == "opencode_cli":
            return OpenCodeCLIProvider(profile)
        if profile.kind == "openai_compatible":
            return OpenAICompatibleProvider(profile, api_key=self.credential(profile))
        raise ModelProviderError("provider_unsupported", "不支持的模型 Provider。")

    def status(self, provider_id: str) -> dict:
        profile = self.get(provider_id)
        if not profile.enabled:
            return {
                "profile": profile.model_dump(mode="json"),
                "available": False,
                "stage": "disabled",
                "detail": "Provider 已停用。",
                "checked_at": _now(),
                "latency_ms": 0,
                "suggestions": ["启用后再执行连接测试。"],
            }
        diagnostic = self.build(profile).diagnose()
        payload = {
            "available": diagnostic.available,
            "stage": diagnostic.stage,
            "detail": diagnostic.detail,
            "checked_at": _now(),
            "latency_ms": diagnostic.latency_ms,
            "tested_url": diagnostic.tested_url,
            "suggested_base_url": diagnostic.suggested_base_url,
            "http_status": diagnostic.http_status,
            "discovered_models": list(diagnostic.discovered_models),
            "model_available": diagnostic.model_available,
            "suggestions": list(diagnostic.suggestions),
        }
        try:
            self.repository.set_health(provider_id, payload)
            result_profile = self.get(provider_id)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).casefold():
                raise
            payload["suggestions"].append(
                "连接测试已完成；数据库正忙，本次健康状态未持久化。"
            )
            result_profile = profile.model_copy(update={
                "last_tested_at": payload["checked_at"],
                "last_test_ok": payload["available"],
                "last_test_detail": payload["detail"],
                "last_test_latency_ms": payload["latency_ms"],
            })
        payload["profile"] = result_profile.model_dump(mode="json")
        return payload

    def capability_status(self) -> dict:
        config = get_config().agent
        profile = self.get()
        if not config.enabled:
            available, detail = False, "自动整理已在配置中关闭。"
        elif not profile.enabled:
            available, detail = False, "默认 Provider 已停用。"
        elif profile.kind in {"codex_cli", "opencode_cli"}:
            available, detail = self.build(profile).available()
        else:
            health = self.repository.get_health(profile.id)
            if health:
                available = bool(health.get("available"))
                detail = str(health.get("detail") or "最近一次连接测试已完成。")
            else:
                available = False
                detail = "当前配置尚未验证；请在模型管理中执行连接测试。"
        effective_model = profile.model
        effective_effort = profile.reasoning_effort
        if profile.kind == "codex_cli":
            defaults = _codex_local_defaults()
            effective_model = effective_model or defaults["model"]
            if effective_effort == "inherit":
                effective_effort = defaults["reasoning_effort"]
        return {
            "enabled": config.enabled,
            "provider": profile.kind,
            "provider_id": profile.id,
            "provider_name": profile.name,
            "available": available,
            "detail": detail,
            "model": effective_model or profile.name,
            "reasoning_effort": effective_effort,
            "use_proxy": profile.use_proxy,
            "timeout_seconds": profile.timeout_seconds,
            "redact_before_send": config.redact_before_send,
            "allowed_sources": config.allowed_sources,
            "excluded_sources": config.excluded_sources,
            "supports": ["session_analysis", "query_planning", "evidence_answer", "review"],
        }

    def codex_catalog(self) -> dict:
        self.initialize()
        profile = self.get(BUILTIN_CODEX_ID)
        executable = profile.executable or "codex"
        from app.agent_provider import _provider_process_env

        try:
            completed = subprocess.run(
                [executable, "debug", "models"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
                env=_provider_process_env(profile),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelProviderError(
                "provider_unavailable", f"无法读取 Codex 模型目录：{exc}"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "未知错误").strip()
            raise ModelProviderError(
                "provider_unavailable", f"Codex 模型目录读取失败：{detail[-1000:]}"
            )
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError) as exc:
            raise ModelProviderError(
                "provider_unavailable", "Codex 没有返回有效的模型目录。"
            ) from exc
        raw_models = payload.get("models") if isinstance(payload, dict) else None
        models = []
        for item in raw_models if isinstance(raw_models, list) else []:
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            slug = str(item.get("slug") or "").strip()
            if not slug:
                continue
            levels = [
                str(level.get("effort"))
                for level in item.get("supported_reasoning_levels") or []
                if isinstance(level, dict) and level.get("effort")
            ]
            models.append({
                "id": slug,
                "name": str(item.get("display_name") or slug),
                "description": str(item.get("description") or ""),
                "default_reasoning_effort": str(
                    item.get("default_reasoning_level") or "medium"
                ),
                "reasoning_efforts": levels,
            })
        defaults = _codex_local_defaults()
        return {
            "models": models,
            "default_model": defaults["model"],
            "default_reasoning_effort": defaults["reasoning_effort"],
        }

    def _decorate(self, row: dict) -> ModelProviderProfile:
        values = dict(row)
        values["enabled"] = bool(values["enabled"])
        values["is_default"] = bool(values["is_default"])
        values["use_proxy"] = bool(values.get("use_proxy"))
        saved = bool(self.repository.get_secret(row["id"]))
        environment = bool(row.get("credential_ref") and os.getenv(row["credential_ref"]))
        health = self.repository.get_health(row["id"])
        return ModelProviderProfile(
            **values,
            credential_configured=saved or environment,
            credential_source="saved" if saved else "environment" if environment else "none",
            managed=row["id"] == BUILTIN_CODEX_ID,
            last_tested_at=health.get("checked_at"),
            last_test_ok=health.get("available"),
            last_test_detail=health.get("detail"),
            last_test_latency_ms=health.get("latency_ms"),
        )


_service: Optional[ModelProviderService] = None
_service_lock = Lock()


def get_model_provider_service() -> ModelProviderService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = ModelProviderService()
    _service.initialize()
    return _service


def initialize_model_providers() -> None:
    get_model_provider_service().initialize()


def _secret_value(value: object) -> Optional[str]:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    raw = getter() if callable(getter) else str(value)
    return raw.strip() or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _codex_local_defaults() -> dict[str, str]:
    """Read only model preferences from Codex config; authentication stays in Codex."""
    try:
        import tomllib

        codex_home = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
        with open(os.path.join(codex_home, "config.toml"), "rb") as handle:
            config = tomllib.load(handle)
    except (OSError, ValueError):
        config = {}
    return {
        "model": str(config.get("model") or ""),
        "reasoning_effort": str(config.get("model_reasoning_effort") or "medium"),
    }
