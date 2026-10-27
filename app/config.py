"""Configuration management."""
import os
import yaml
from typing import List, Optional
from dataclasses import dataclass, field

CONFIG_PATH = os.environ.get("AM_CONFIG", "config.yaml")

@dataclass
class ServerConfig:
    name: str
    host: str
    port: int = 22
    user: str = ""
    password: str = ""
    password_env: str = ""
    copilot_dir: str = "~/.copilot/session-state"
    codex_dir: str = "~/.codex"
    claude_dir: str = "~/.claude"
    claude_projects_dir: str = ""
    opencode_dir: str = "~/.local/share/opencode"
    vscode_dir: str = "~/.vscode-server"
    enabled: bool = True
    is_local: bool = False
    is_mac: bool = False

@dataclass
class LocalConfig:
    copilot_dir: str = "~/.copilot/session-state"
    codex_dir: str = "~/.codex"
    claude_dir: str = "~/.claude"
    claude_projects_dir: str = ""
    opencode_dir: str = ""
    vscode_dir: str = "~/.vscode-server"
    mac_chat_sessions_dir: str = ""

@dataclass
class SyncConfig:
    interval_minutes: int = 5
    auto_start: bool = True
    temp_dir: str = "/tmp/agent-memory-sync"
    max_run_seconds: int = 1800

@dataclass
class DatabaseConfig:
    path: str = "./data/agent_memory.db"

@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 18080


@dataclass
class AuthConfig:
    """Authentication policy for the HTTP service."""

    enabled: bool = False
    session_hours: int = 12
    cookie_secure: bool = False
    allowed_origins: List[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    """Configuration for the derived work-memory intelligence layer.

    CLI providers reuse an existing local login/configuration. The provider
    boundary keeps enrichment and search independent from a specific agent.
    """

    enabled: bool = True
    provider: str = "codex_cli"
    executable: str = "codex"
    model: str = ""
    reasoning_effort: str = "inherit"
    timeout_seconds: int = 600
    max_input_chars: int = 12000
    max_segments_per_session: int = 2
    settle_seconds: int = 300
    auto_enrich: bool = True
    auto_start_worker: bool = True
    backfill_on_start: bool = False
    recent_backfill_days: int = 90
    auto_approve_threshold: float = 0.92
    redact_before_send: bool = True
    allowed_sources: List[str] = field(default_factory=list)
    excluded_sources: List[str] = field(default_factory=list)

@dataclass
class AppConfig:
    servers: List[ServerConfig] = field(default_factory=list)
    local: LocalConfig = field(default_factory=LocalConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    web: WebConfig = field(default_factory=WebConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)


def _expand_secret(value: str) -> str:
    """Resolve ``${ENV_NAME}`` without introducing a .env dependency."""
    if not isinstance(value, str):
        return value or ""
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value

def load_config(path: str = None) -> AppConfig:
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        return AppConfig()
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    servers = []
    for item in raw.get("servers", []):
        server_data = dict(item)
        password_env = server_data.get("password_env", "")
        if password_env:
            server_data["password"] = os.environ.get(password_env, "")
        else:
            server_data["password"] = _expand_secret(
                server_data.get("password", "")
            )
        servers.append(ServerConfig(**server_data))
    local = LocalConfig(**raw.get("local", {}))
    sync = SyncConfig(**raw.get("sync", {}))
    database = DatabaseConfig(**raw.get("database", {}))
    web = WebConfig(**raw.get("web", {}))
    auth = AuthConfig(**raw.get("auth", {}))
    agent = AgentConfig(**raw.get("agent", {}))
    return AppConfig(servers=servers, local=local, sync=sync,
                     database=database, web=web, auth=auth, agent=agent)

_config: Optional[AppConfig] = None

def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config

def reload_config():
    global _config
    _config = load_config()
    return _config
