"""SSH-based remote sync for conversation data."""
import json
import hashlib
import os
import shutil
import logging
import shlex
import stat
import uuid
from typing import List, Optional
from datetime import datetime

import paramiko

from app.config import ServerConfig, get_config
from app.sync.device_store import get_sync_server_configs
from app.database import (
    enrich_session_metadata,
    get_db,
    insert_messages,
    replace_session_with_messages,
    upsert_session,
)
from app.parsers.copilot import parse_copilot_session, parse_copilot_coding_agent
from app.parsers.codex import (
    find_codex_rollout_files,
    merge_codex_subagents,
    parse_codex_index,
    parse_codex_session,
    scan_codex_rollout_file,
)
from app.parsers.claude import parse_claude_session, find_claude_session_files
from app.parsers.opencode import parse_opencode_groups, scan_opencode_source
from app.parsers.vscode_chat import (
    parse_vscode_chat_edits,
    parse_vscode_session_metadata,
    parse_vscode_memory_files,
)
from app.parsers.vscode_mac_chat import iter_mac_chat_sessions

logger = logging.getLogger(__name__)
_CODEX_SOURCE_SIGNATURE_VERSION = "codex-parser-v2-omx-merge"
_SSH_CONNECT_TIMEOUT_SECONDS = 30
_SSH_COMMAND_TIMEOUT_SECONDS = 120
_SFTP_READ_TIMEOUT_SECONDS = 120
_OPENCODE_SNAPSHOT_SCRIPT = """import os
import pathlib
import sqlite3
import sys

uri = pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro"
source = sqlite3.connect(uri, uri=True, timeout=5)
source.execute("PRAGMA query_only=ON")
source.execute("BEGIN")
snapshot = sqlite3.connect(sys.argv[2])
for table in ("project", "session", "message", "part"):
    row = source.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name=? AND sql IS NOT NULL",
        (table,),
    ).fetchone()
    if not row:
        continue
    snapshot.execute(row[0])
    columns = [item[1] for item in source.execute(f"PRAGMA table_info('{table}')")]
    rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
    if rows:
        placeholders = ",".join("?" for _ in columns)
        names = ",".join(f'"{column}"' for column in columns)
        snapshot.executemany(
            f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
            rows,
        )
snapshot.commit()
snapshot.close()
source.close()
os.chmod(sys.argv[2], 0o600)
"""


def _get_session_metadata(session_id: str) -> tuple[bool, dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT metadata_json FROM sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
    if not row:
        return False, {}
    try:
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return True, metadata


def _should_refresh_file_backed_session(
    session_id: str,
    source_mtime: int,
    source_size: int,
    force: bool = False,
) -> tuple[bool, bool]:
    existing, metadata = _get_session_metadata(session_id)
    if force or not existing:
        return True, existing

    if metadata.get("source_file_mtime") != source_mtime:
        return True, existing
    if metadata.get("source_file_size") != source_size:
        return True, existing
    return False, existing


def _build_source_signature(source_files: list[dict]) -> str:
    normalized = []
    for item in source_files or []:
        normalized.append({
            "path": item.get("path", ""),
            "mtime": int(item.get("mtime", 0) or 0),
            "size": int(item.get("size", 0) or 0),
        })
    normalized.sort(key=lambda item: item["path"])
    raw = json.dumps({
        "version": _CODEX_SOURCE_SIGNATURE_VERSION,
        "files": normalized,
    }, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def _should_refresh_signature_backed_session(
    session_id: str,
    source_signature: str,
    force: bool = False,
) -> tuple[bool, bool]:
    existing, metadata = _get_session_metadata(session_id)
    if force or not existing:
        return True, existing
    return metadata.get("source_signature") != source_signature, existing


def _discover_mac_ip(configured_host: str) -> Optional[str]:
    """Discover Mac's current IP from active SSH connections to this server.

    Uses the configured (possibly stale) host IP to determine the expected
    subnet prefix, then finds an active connection from that subnet.
    """
    import subprocess
    import re

    parts = configured_host.split(".")
    if len(parts) < 2:
        return None
    prefix = f"{parts[0]}.{parts[1]}."

    # Try `who` first — shows logged-in users with source IPs
    try:
        result = subprocess.run(
            ["who"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            match = re.search(r"\(([\d.]+)\)", line)
            if match:
                ip = match.group(1)
                if ip.startswith(prefix):
                    return ip
    except Exception:
        pass

    # Fallback: scan /proc/*/environ for SSH_CLIENT in the same subnet
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            environ_path = f"/proc/{entry}/environ"
            try:
                with open(environ_path, "rb") as f:
                    data = f.read()
                for var in data.split(b"\x00"):
                    if var.startswith(b"SSH_CLIENT="):
                        ip = var.decode(errors="ignore").split("=", 1)[1].split()[0]
                        if ip.startswith(prefix):
                            return ip
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    except Exception:
        pass

    return None


def _local_file_fingerprint(path: str) -> tuple[int, int]:
    file_stat = os.stat(path)
    return int(file_stat.st_mtime or 0), int(file_stat.st_size or 0)


def _source_sessions_exist(session_ids: list[str]) -> bool:
    if not session_ids:
        return False
    with get_db() as conn:
        for index in range(0, len(session_ids), 400):
            chunk = session_ids[index:index + 400]
            placeholders = ",".join("?" for _ in chunk)
            count = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE id IN ({placeholders})",
                chunk,
            ).fetchone()[0]
            if count != len(chunk):
                return False
    return True


def _load_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_private_json(path: str, value: dict) -> None:
    temporary = path + f".{uuid.uuid4().hex}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, path)


def _claude_projects_dir(config_obj) -> str:
    projects_dir = getattr(config_obj, "claude_projects_dir", "") or ""
    if projects_dir:
        return projects_dir
    claude_dir = getattr(config_obj, "claude_dir", "~/.claude") or "~/.claude"
    return os.path.join(claude_dir, "projects")


def _sync_opencode_source(source: Optional[dict], server_name: str,
                          force: bool, stats: dict) -> None:
    if not source:
        return
    changed_root_ids = []
    for group in source.get("groups", []):
        session_id = f"opencode-{server_name}-{group['session_id']}"
        should_sync, _existing = _should_refresh_signature_backed_session(
            session_id, group["source_signature"], force=force
        )
        if should_sync:
            changed_root_ids.append(group["session_id"])
        else:
            stats["skipped"] += 1
    if not changed_root_ids:
        return

    signatures = {
        group["session_id"]: group["source_signature"]
        for group in source.get("groups", [])
    }
    try:
        parsed_results = parse_opencode_groups(
            source, server_name, root_ids=changed_root_ids
        )
    except Exception as exc:
        logger.error(f"Error parsing OpenCode source {source.get('path')}: {exc}")
        stats["errors"] += len(changed_root_ids)
        return

    parsed_by_root = {
        result["session"]["metadata"]["opencode_session_id"]: result
        for result in parsed_results
    }
    for root_id in changed_root_ids:
        result = parsed_by_root.get(root_id)
        if not result:
            logger.warning(f"OpenCode root session disappeared during sync: {root_id}")
            stats["errors"] += 1
            continue
        try:
            metadata = result["session"].setdefault("metadata", {})
            metadata["source_signature"] = signatures[root_id]
            metadata["source_schema_fingerprint"] = source.get(
                "schema_fingerprint"
            )
            metadata["source_path"] = (
                source.get("origin_path") or source.get("path")
            )
            metadata["parser_version"] = "opencode-parser-v1"
            # OpenCode tool parts are mutable while commands run. Commit the
            # new signature and message replacement in one transaction.
            replace_session_with_messages(
                result["session"], result["messages"]
            )
            stats["synced"] += 1
        except Exception as exc:
            logger.error(f"Error importing OpenCode session {root_id}: {exc}")
            stats["errors"] += 1


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _group_codex_descriptors(descriptors: list[dict]) -> list[list[dict]]:
    """Group rollout descriptors without retaining their full messages."""
    root_groups: dict[str, list[dict]] = {}
    explicit_children = []
    for descriptor in descriptors:
        if descriptor.get("parent_thread_id"):
            explicit_children.append(descriptor)
        else:
            key = descriptor.get("codex_id") or descriptor["uuid"]
            root_groups.setdefault(key, []).append(descriptor)

    for descriptor in explicit_children:
        parent_id = descriptor.get("parent_thread_id")
        if parent_id in root_groups:
            root_groups[parent_id].append(descriptor)
        else:
            root_groups.setdefault(
                descriptor.get("codex_id") or descriptor["uuid"], []
            ).append(descriptor)

    # Reproduce the common OMX team merge using lightweight header metadata.
    controllers = []
    candidates = []
    for key, group in root_groups.items():
        root = group[0]
        if root.get("team_kind") == "controller":
            controllers.append((key, root))
        elif root.get("team_kind") in {"worker", "helper"}:
            candidates.append((key, root))

    merged_child_keys = set()
    for child_key, child in candidates:
        child_start = _parse_iso_timestamp(child.get("start_time"))
        best = None
        best_gap = None
        for controller_key, controller in controllers:
            if controller.get("team_root") != child.get("team_root"):
                continue
            controller_start = _parse_iso_timestamp(controller.get("start_time"))
            if child_start and controller_start:
                gap = (child_start - controller_start).total_seconds()
                if gap < -600 or gap > 10800:
                    continue
                abs_gap = abs(gap)
            else:
                abs_gap = 0
            if best_gap is None or abs_gap < best_gap:
                best = controller_key
                best_gap = abs_gap
        if best and best != child_key:
            root_groups[best].extend(root_groups[child_key])
            merged_child_keys.add(child_key)

    return [
        group for key, group in root_groups.items()
        if key not in merged_child_keys
    ]


def _descriptor_source_file(descriptor: dict) -> dict:
    return {
        "path": descriptor.get("signature_path") or descriptor["path"],
        "mtime": int(descriptor.get("signature_mtime",
                                    descriptor.get("mtime", 0)) or 0),
        "size": int(descriptor.get("size", 0) or 0),
    }


def _sync_codex_cached_files(file_descriptors: list[dict], server_name: str,
                              index_entries: dict, force: bool,
                              stats: dict) -> None:
    """Plan from headers, then fully parse only changed conversation groups."""
    scanned = []
    for file_info in file_descriptors:
        try:
            filename = os.path.basename(file_info["path"])
            parts = filename.replace(".jsonl", "").split("-")
            uuid_part = "-".join(parts[-5:]) if len(parts) >= 5 else filename
            descriptor = scan_codex_rollout_file(
                file_info["path"], index_entries.get(uuid_part)
            )
            if descriptor:
                if file_info.get("signature_path") is not None:
                    descriptor["signature_path"] = file_info["signature_path"]
                if file_info.get("signature_mtime") is not None:
                    descriptor["signature_mtime"] = file_info["signature_mtime"]
                scanned.append(descriptor)
        except Exception as exc:
            logger.error(f"Error scanning codex rollout {file_info['path']}: {exc}")
            stats["errors"] += 1

    for group in _group_codex_descriptors(scanned):
        root = next(
            (item for item in group if not item.get("parent_thread_id")),
            group[0],
        )
        expected_session_id = f"codex-{server_name}-{root['uuid']}"
        source_files = [_descriptor_source_file(item) for item in group]
        source_signature = _build_source_signature(source_files)
        should_sync, _ = _should_refresh_signature_backed_session(
            expected_session_id, source_signature, force=force
        )
        if not should_sync:
            stats["skipped"] += 1
            continue

        parsed = []
        for descriptor in group:
            try:
                result = parse_codex_session(
                    descriptor["path"],
                    server_name,
                    index_entries.get(descriptor["uuid"]),
                )
                if result:
                    result["_source_files"] = [
                        _descriptor_source_file(descriptor)
                    ]
                    parsed.append(result)
            except Exception as exc:
                logger.error(
                    f"Error parsing codex rollout {descriptor['path']}: {exc}"
                )
                stats["errors"] += 1
        for result in merge_codex_subagents(parsed):
            sid = result["session"]["id"]
            metadata = result["session"].setdefault("metadata", {})
            result_signature = _build_source_signature(
                result.get("_source_files", [])
            )
            should_import, existing = _should_refresh_signature_backed_session(
                sid, result_signature, force=force
            )
            if not should_import:
                stats["skipped"] += 1
                continue
            try:
                metadata["source_signature"] = result_signature
                metadata["source_file_count"] = len(
                    result.get("_source_files", [])
                )
                upsert_session(result["session"])
                insert_messages(
                    sid,
                    result["messages"],
                    force=(force or existing),
                    incremental=(existing and not force),
                )
                stats["synced"] += 1
            except Exception as exc:
                logger.error(f"Error importing codex session {sid}: {exc}")
                stats["errors"] += 1


def _filter_changed_mac_chat_files(filepaths: list[str], server_name: str,
                                   force: bool) -> tuple[list[str], int]:
    """Filter snapshots by DB fingerprints before replaying large JSONLs."""
    if force:
        return filepaths, 0
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, metadata_json FROM sessions
               WHERE source = 'vscode-chat' AND server_name = ?""",
            (server_name,),
        ).fetchall()
    existing = {}
    for row in rows:
        try:
            existing[row["id"]] = (
                json.loads(row["metadata_json"])
                if row["metadata_json"] else {}
            )
        except (json.JSONDecodeError, TypeError):
            existing[row["id"]] = {}

    changed = []
    skipped = 0
    for filepath in filepaths:
        try:
            file_stat = os.stat(filepath)
        except OSError:
            continue
        # The remote collector intentionally ignores these incomplete stubs.
        if file_stat.st_size <= 2048:
            skipped += 1
            continue
        uuid = os.path.splitext(os.path.basename(filepath))[0]
        session_id = f"vscode-chat-{server_name}-{uuid}"
        metadata = existing.get(session_id)
        if (
            metadata
            and metadata.get("source_file_mtime") == int(file_stat.st_mtime or 0)
            and metadata.get("source_file_size") == int(file_stat.st_size or 0)
        ):
            skipped += 1
        else:
            changed.append(filepath)
    return changed, skipped


class SSHSyncer:
    def __init__(self, server: ServerConfig):
        self.server = server
        self.client: Optional[paramiko.SSHClient] = None
        self.sftp: Optional[paramiko.SFTPClient] = None

    def connect(self):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        host = self.server.host
        if getattr(self.server, 'is_mac', False):
            discovered = _discover_mac_ip(self.server.host)
            if discovered and discovered != host:
                logger.info(f"Auto-discovered Mac IP: {discovered} (config has {host})")
                host = discovered
        self.client.connect(
            hostname=host,
            port=self.server.port,
            username=self.server.user,
            password=self.server.password,
            timeout=_SSH_CONNECT_TIMEOUT_SECONDS,
            banner_timeout=_SSH_CONNECT_TIMEOUT_SECONDS,
            auth_timeout=_SSH_CONNECT_TIMEOUT_SECONDS,
        )
        self.sftp = self.client.open_sftp()
        self.sftp.get_channel().settimeout(_SFTP_READ_TIMEOUT_SECONDS)
        transport = self.client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        logger.info(f"Connected to {self.server.name} ({host}:{self.server.port})")

    def disconnect(self):
        try:
            if self.sftp:
                self.sftp.close()
        except Exception:
            logger.debug("SFTP close failed for %s", self.server.name, exc_info=True)
        try:
            if self.client:
                self.client.close()
        except Exception:
            logger.debug("SSH close failed for %s", self.server.name, exc_info=True)
        self.sftp = None
        self.client = None
        logger.info(f"Disconnected from {self.server.name}")

    def _exec_command(self, command: str, timeout: int = _SSH_COMMAND_TIMEOUT_SECONDS):
        """Execute a bounded remote command.

        Paramiko's ``connect(timeout=...)`` only bounds connection setup.  A
        remote command may otherwise wait forever for EOF, which used to pin
        the global sync job indefinitely.
        """
        if self.client is None:
            raise RuntimeError("SSH client is not connected")
        return self.client.exec_command(command, timeout=max(1, int(timeout)))

    def _resolve_remote_path(self, path: str) -> str:
        if path.startswith("~"):
            stdin, stdout, stderr = self._exec_command("echo $HOME")
            home = stdout.read().decode().strip()
            path = path.replace("~", home, 1)
        return path

    def _remote_ls(self, path: str) -> List[str]:
        try:
            return self.sftp.listdir(path)
        except FileNotFoundError:
            return []

    def _download_file(self, remote_path: str, local_path: str):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        self.sftp.get(remote_path, local_path)
        try:
            remote_stat = self.sftp.stat(remote_path)
            remote_mtime = int(getattr(remote_stat, "st_mtime", 0) or 0)
            if remote_mtime:
                os.utime(local_path, (remote_mtime, remote_mtime))
        except Exception:
            pass

    def _remote_file_exists(self, path: str) -> bool:
        try:
            self.sftp.stat(path)
            return True
        except FileNotFoundError:
            return False

    def sync_copilot(self, force: bool = False) -> dict:
        """Sync Copilot sessions from remote server. If force=True, re-download and re-import all."""
        stats = {"synced": 0, "skipped": 0, "errors": 0}
        remote_dir = self._resolve_remote_path(self.server.copilot_dir)

        try:
            session_dirs = self._remote_ls(remote_dir)
        except Exception as e:
            logger.error(f"Cannot list {remote_dir}: {e}")
            stats["errors"] += 1
            return stats

        config = get_config()
        temp_base = config.sync.temp_dir

        for session_id in session_dirs:
            remote_session_dir = f"{remote_dir}/{session_id}"
            remote_events = f"{remote_session_dir}/events.jsonl"
            remote_workspace = f"{remote_session_dir}/workspace.yaml"

            if self._remote_file_exists(remote_events):
                composite_id = f"copilot-{self.server.name}-{session_id}"
                remote_stat = self.sftp.stat(remote_events)
                source_mtime = int(getattr(remote_stat, "st_mtime", 0) or 0)
                source_size = int(getattr(remote_stat, "st_size", 0) or 0)
                should_sync, existing = _should_refresh_file_backed_session(
                    composite_id,
                    source_mtime,
                    source_size,
                    force=force,
                )
                if not should_sync:
                    stats["skipped"] += 1
                    continue

                local_session_dir = os.path.join(temp_base, self.server.name, "copilot", session_id)
                os.makedirs(local_session_dir, exist_ok=True)
                local_events = os.path.join(local_session_dir, "events.jsonl")

                try:
                    self._download_file(remote_events, local_events)
                    result = parse_copilot_session(local_session_dir, self.server.name)
                    if result:
                        metadata = result["session"].setdefault("metadata", {})
                        metadata["source_file_mtime"] = source_mtime
                        metadata["source_file_size"] = source_size
                        upsert_session(result["session"])
                        insert_messages(
                            result["session"]["id"], result["messages"],
                            force=(force or existing),
                            incremental=(existing and not force),
                        )
                        stats["synced"] += 1
                        logger.info(f"Synced copilot session: {session_id}")
                except Exception as e:
                    logger.error(f"Error syncing copilot session {session_id}: {e}")
                    stats["errors"] += 1

            elif self._remote_file_exists(remote_workspace):
                composite_id = f"copilot-agent-{self.server.name}-{session_id}"
                remote_stat = self.sftp.stat(remote_workspace)
                source_mtime = int(getattr(remote_stat, "st_mtime", 0) or 0)
                source_size = int(getattr(remote_stat, "st_size", 0) or 0)
                should_sync, existing = _should_refresh_file_backed_session(
                    composite_id,
                    source_mtime,
                    source_size,
                    force=force,
                )
                if not should_sync:
                    stats["skipped"] += 1
                    continue

                local_session_dir = os.path.join(temp_base, self.server.name, "copilot", session_id)
                os.makedirs(local_session_dir, exist_ok=True)
                local_workspace = os.path.join(local_session_dir, "workspace.yaml")

                try:
                    self._download_file(remote_workspace, local_workspace)
                    # Download checkpoints if present
                    remote_checkpoints = f"{remote_session_dir}/checkpoints"
                    try:
                        cp_files = self._remote_ls(remote_checkpoints)
                        if cp_files:
                            local_cp_dir = os.path.join(local_session_dir, "checkpoints")
                            os.makedirs(local_cp_dir, exist_ok=True)
                            for cp_file in cp_files:
                                self._download_file(
                                    f"{remote_checkpoints}/{cp_file}",
                                    os.path.join(local_cp_dir, cp_file),
                                )
                    except Exception:
                        pass
                    result = parse_copilot_coding_agent(local_session_dir, self.server.name)
                    if result:
                        metadata = result["session"].setdefault("metadata", {})
                        metadata["source_file_mtime"] = source_mtime
                        metadata["source_file_size"] = source_size
                        upsert_session(result["session"])
                        insert_messages(
                            result["session"]["id"], result["messages"],
                            force=(force or existing),
                            incremental=(existing and not force),
                        )
                        stats["synced"] += 1
                        logger.info(f"Synced copilot coding agent session: {session_id}")
                except Exception as e:
                    logger.error(f"Error syncing copilot coding agent session {session_id}: {e}")
                    stats["errors"] += 1

        return stats

    def sync_codex(self, force: bool = False) -> dict:
        """Sync Codex sessions from remote server with subagent/rollout merging."""
        stats = {"synced": 0, "skipped": 0, "errors": 0}
        remote_codex_dir = self._resolve_remote_path(self.server.codex_dir)
        config = get_config()
        temp_base = config.sync.temp_dir

        # Download session_index.jsonl first
        remote_index = f"{remote_codex_dir}/session_index.jsonl"
        local_index_dir = os.path.join(temp_base, self.server.name, "codex")
        os.makedirs(local_index_dir, exist_ok=True)
        local_index = os.path.join(local_index_dir, "session_index.jsonl")

        index_entries = {}
        try:
            if self._remote_file_exists(remote_index):
                self._download_file(remote_index, local_index)
                entries = parse_codex_index(local_index_dir)
                index_entries = {e["id"]: e for e in entries}
        except Exception as e:
            logger.warning(f"Could not download codex index: {e}")

        # Find and download rollout files
        remote_sessions_dir = f"{remote_codex_dir}/sessions"
        try:
            stdin, stdout, stderr = self._exec_command(
                f"find {remote_sessions_dir} -name 'rollout-*.jsonl' 2>/dev/null"
            )
            remote_files = [
                line.strip()
                for line in stdout.read().decode().splitlines()
                if line.strip()
            ]
        except Exception as e:
            logger.error(f"Cannot find codex sessions: {e}")
            stats["errors"] += 1
            return stats

        # Keep a local fingerprinted cache; unchanged remote rollouts are not
        # downloaded again and only changed conversation groups are parsed.
        cached_files = []
        for remote_file in remote_files:
            filename = os.path.basename(remote_file)
            local_file = os.path.join(local_index_dir, "sessions", filename)
            os.makedirs(os.path.dirname(local_file), exist_ok=True)
            try:
                remote_stat = self.sftp.stat(remote_file)
                remote_size = int(getattr(remote_stat, "st_size", 0) or 0)
                remote_mtime = int(getattr(remote_stat, "st_mtime", 0) or 0)
                cache_matches = (
                    os.path.exists(local_file)
                    and int(os.path.getsize(local_file) or 0) == remote_size
                    and int(os.path.getmtime(local_file) or 0) == remote_mtime
                )
                if force or not cache_matches:
                    self._download_file(remote_file, local_file)
                cached_files.append({
                    "path": local_file,
                    "signature_path": remote_file,
                    # Preserve the previous remote signature format (size was
                    # the change detector and mtime was intentionally zero).
                    "signature_mtime": 0,
                })
            except Exception as exc:
                logger.error(f"Error caching codex session {filename}: {exc}")
                stats["errors"] += 1
        _sync_codex_cached_files(
            cached_files,
            self.server.name,
            index_entries,
            force,
            stats,
        )

        return stats

    def sync_claude(self, force: bool = False) -> dict:
        """Sync Claude project JSONL sessions from remote server."""
        stats = {"synced": 0, "skipped": 0, "errors": 0}
        remote_projects_dir = self._resolve_remote_path(_claude_projects_dir(self.server))
        config = get_config()
        temp_base = config.sync.temp_dir

        try:
            stdin, stdout, stderr = self._exec_command(
                f"find {remote_projects_dir} -mindepth 2 -maxdepth 2 -name '*.jsonl' 2>/dev/null"
            )
            remote_files = [
                line.strip()
                for line in stdout.read().decode().splitlines()
                if line.strip()
            ]
        except Exception as e:
            logger.warning(f"Cannot find claude sessions: {e}")
            stats["errors"] += 1
            return stats

        for remote_file in remote_files:
            filename = os.path.basename(remote_file)
            session_uuid = os.path.splitext(filename)[0]
            composite_id = f"claude-{self.server.name}-{session_uuid}"
            try:
                remote_stat = self.sftp.stat(remote_file)
                source_mtime = int(getattr(remote_stat, "st_mtime", 0) or 0)
                source_size = int(getattr(remote_stat, "st_size", 0) or 0)
                should_sync, existing = _should_refresh_file_backed_session(
                    composite_id,
                    source_mtime,
                    source_size,
                    force=force,
                )
                if not should_sync:
                    stats["skipped"] += 1
                    continue

                project_name = os.path.basename(os.path.dirname(remote_file))
                local_file = os.path.join(temp_base, self.server.name, "claude", project_name, filename)
                self._download_file(remote_file, local_file)
                result = parse_claude_session(local_file, self.server.name)
                if result:
                    metadata = result["session"].setdefault("metadata", {})
                    metadata["source_file_mtime"] = source_mtime
                    metadata["source_file_size"] = source_size
                    metadata["remote_path"] = remote_file
                    upsert_session(result["session"])
                    insert_messages(
                        result["session"]["id"], result["messages"],
                        force=(force or existing),
                        incremental=(existing and not force),
                    )
                    stats["synced"] += 1
            except Exception as e:
                logger.error(f"Error syncing claude session {remote_file}: {e}")
                stats["errors"] += 1

        return stats

    def sync_opencode(self, force: bool = False) -> dict:
        """Create a sanitized remote OpenCode snapshot and import sessions."""
        stats = {"synced": 0, "skipped": 0, "errors": 0}
        configured = getattr(
            self.server, "opencode_dir", "~/.local/share/opencode"
        ) or "~/.local/share/opencode"
        remote_path = self._resolve_remote_path(configured)
        if remote_path.endswith(".db"):
            remote_root = os.path.dirname(remote_path)
            remote_db = remote_path
        else:
            remote_root = remote_path
            remote_db = f"{remote_root}/opencode.db"

        temp_root = os.path.join(
            get_config().sync.temp_dir, self.server.name, "opencode"
        )
        os.makedirs(temp_root, exist_ok=True)
        os.chmod(temp_root, stat.S_IRWXU)
        local_db = os.path.join(temp_root, "opencode.db")
        fingerprint_path = os.path.join(temp_root, "source-fingerprint.json")

        if self._remote_file_exists(remote_db):
            remote_fingerprint = {}
            for path in (remote_db, remote_db + "-wal"):
                try:
                    file_stat = self.sftp.stat(path)
                    remote_fingerprint[path] = {
                        "mtime": int(getattr(file_stat, "st_mtime", 0) or 0),
                        "size": int(getattr(file_stat, "st_size", 0) or 0),
                    }
                except FileNotFoundError:
                    remote_fingerprint[path] = None
            cached = _load_json_file(fingerprint_path)
            cached_ids = cached.get("session_ids") or []
            if (
                not force
                and cached.get("fingerprint") == remote_fingerprint
                and isinstance(cached_ids, list)
                and _source_sessions_exist(cached_ids)
            ):
                stats["skipped"] = len(cached_ids)
                return stats

            snapshot_id = uuid.uuid4().hex
            remote_snapshot_dir = f"/tmp/agent-memory-opencode-{snapshot_id}"
            remote_snapshot = f"{remote_snapshot_dir}/opencode.db"
            # Copy only transcript tables. OpenCode's database also contains
            # credentials and share secrets which must never leave the host.
            command = (
                "mkdir -m 700 -- " + shlex.quote(remote_snapshot_dir)
                + " && python3 -c " + shlex.quote(_OPENCODE_SNAPSHOT_SCRIPT)
                + " " + shlex.quote(remote_db)
                + " " + shlex.quote(remote_snapshot)
            )
            try:
                try:
                    os.unlink(local_db)
                except FileNotFoundError:
                    pass
                stdin, stdout, stderr = self._exec_command(command, timeout=300)
                exit_code = stdout.channel.recv_exit_status()
                error = stderr.read().decode(errors="replace").strip()
                if exit_code != 0:
                    raise RuntimeError(error or f"remote backup exit={exit_code}")
                self._download_file(remote_snapshot, local_db)
                os.chmod(local_db, stat.S_IRUSR | stat.S_IWUSR)
                source = scan_opencode_source(local_db)
                if source:
                    source["origin_path"] = remote_db
                _sync_opencode_source(
                    source, self.server.name, force, stats
                )
                if stats["errors"] == 0:
                    root_ids = [
                        f"opencode-{self.server.name}-{group['session_id']}"
                        for group in (source or {}).get("groups", [])
                    ]
                    _write_private_json(fingerprint_path, {
                        "fingerprint": remote_fingerprint,
                        "session_ids": root_ids,
                    })
            except Exception as exc:
                logger.error(
                    f"Error syncing remote OpenCode database from "
                    f"{self.server.name}: {exc}"
                )
                stats["errors"] += 1
            finally:
                try:
                    stdin, stdout, stderr = self._exec_command(
                        "rm -rf -- " + shlex.quote(remote_snapshot_dir)
                    )
                    stdout.channel.recv_exit_status()
                except Exception:
                    pass
                try:
                    os.unlink(local_db)
                except OSError:
                    pass
            return stats

        # Legacy JSON is file-backed and immutable per record. Mirror it when
        # no current SQLite database is present.
        remote_storage = f"{remote_root}/storage"
        local_storage = os.path.join(temp_root, "storage")
        try:
            try:
                os.unlink(fingerprint_path)
            except FileNotFoundError:
                pass
            try:
                os.unlink(local_db)
            except FileNotFoundError:
                pass
            stdin, stdout, stderr = self._exec_command(
                "find " + shlex.quote(remote_storage)
                + " -type f \\( -name '*.json' -o -name migration \\) 2>/dev/null"
            )
            remote_files = [
                line.strip()
                for line in stdout.read().decode(errors="replace").splitlines()
                if line.strip()
            ]
            if os.path.isdir(local_storage):
                shutil.rmtree(local_storage)
            os.makedirs(local_storage, mode=0o700)
            for remote_file in remote_files:
                relative = os.path.relpath(remote_file, remote_storage)
                local_file = os.path.join(local_storage, relative)
                try:
                    remote_stat = self.sftp.stat(remote_file)
                    remote_size = int(getattr(remote_stat, "st_size", 0) or 0)
                    remote_mtime = int(getattr(remote_stat, "st_mtime", 0) or 0)
                    cache_matches = (
                        os.path.exists(local_file)
                        and os.path.getsize(local_file) == remote_size
                        and int(os.path.getmtime(local_file) or 0) == remote_mtime
                    )
                    if force or not cache_matches:
                        self._download_file(remote_file, local_file)
                except Exception as exc:
                    logger.warning(f"Cannot cache OpenCode file {remote_file}: {exc}")
                    stats["errors"] += 1
            source = scan_opencode_source(local_storage)
            if source:
                source["origin_path"] = remote_storage
            _sync_opencode_source(source, self.server.name, force, stats)
        except Exception as exc:
            logger.error(
                f"Error syncing legacy OpenCode storage from "
                f"{self.server.name}: {exc}"
            )
            stats["errors"] += 1
        return stats

    def sync_vscode_metadata(self, force: bool = False) -> dict:
        """Download VS Code session metadata and enrich existing copilot sessions."""
        stats = {"enriched": 0, "errors": 0}
        remote_vscode_dir = self._resolve_remote_path(self.server.vscode_dir)
        remote_meta = (
            f"{remote_vscode_dir}/data/User/globalStorage/github.copilot-chat"
            f"/copilotcli/copilotcli.session.metadata.json"
        )
        if not self._remote_file_exists(remote_meta):
            return stats

        config = get_config()
        temp_base = config.sync.temp_dir
        local_meta_dir = os.path.join(temp_base, self.server.name, "vscode")
        os.makedirs(local_meta_dir, exist_ok=True)
        local_meta = os.path.join(local_meta_dir, "copilotcli.session.metadata.json")

        try:
            self._download_file(remote_meta, local_meta)
            # parse_vscode_session_metadata expects the vscode root dir; build a
            # temporary structure so the relative path resolves correctly.
            fake_vscode_root = os.path.join(local_meta_dir, "_vscode_root")
            dst_dir = os.path.join(
                fake_vscode_root,
                "data/User/globalStorage/github.copilot-chat/copilotcli",
            )
            os.makedirs(dst_dir, exist_ok=True)
            dst_file = os.path.join(dst_dir, "copilotcli.session.metadata.json")
            shutil.copy2(local_meta, dst_file)

            metadata = parse_vscode_session_metadata(fake_vscode_root)
            for session_uuid, meta in metadata.items():
                try:
                    enrich_session_metadata(
                        session_uuid,
                        workspace_folder=meta.get("workspace_folder"),
                        first_user_message=meta.get("first_user_message"),
                    )
                    stats["enriched"] += 1
                except Exception as e:
                    logger.warning(f"Error enriching session {session_uuid}: {e}")
                    stats["errors"] += 1
        except Exception as e:
            logger.error(f"Error syncing vscode metadata from {self.server.name}: {e}")
            stats["errors"] += 1

        return stats

    def sync_vscode_memories(self, force: bool = False) -> dict:
        """Download and import VS Code memory files."""
        stats = {"synced": 0, "skipped": 0, "errors": 0}
        remote_vscode_dir = self._resolve_remote_path(self.server.vscode_dir)
        config = get_config()
        temp_base = config.sync.temp_dir

        # Find memory .md files on remote
        remote_pattern = (
            f"{remote_vscode_dir}/data/User/workspaceStorage"
            f"/*/GitHub.copilot-chat/memory-tool/memories"
        )
        try:
            stdin, stdout, stderr = self._exec_command(
                f"find {remote_pattern} -name '*.md' 2>/dev/null"
            )
            remote_files = [
                line.strip()
                for line in stdout.read().decode().splitlines()
                if line.strip()
            ]
        except Exception as e:
            logger.warning(f"Cannot find vscode memory files: {e}")
            return stats

        if not remote_files:
            return stats

        # Download into a temp structure that mirrors the remote layout
        local_vscode_root = os.path.join(temp_base, self.server.name, "vscode_memories_root")
        for remote_file in remote_files:
            # Reconstruct relative path under vscode_dir
            rel = remote_file
            if remote_vscode_dir in rel:
                rel = rel[len(remote_vscode_dir):]
            local_file = os.path.join(local_vscode_root, rel.lstrip("/"))
            try:
                os.makedirs(os.path.dirname(local_file), exist_ok=True)
                self._download_file(remote_file, local_file)
            except Exception as e:
                logger.warning(f"Cannot download memory file {remote_file}: {e}")
                stats["errors"] += 1

        # Parse downloaded files
        results = parse_vscode_memory_files(local_vscode_root, self.server.name)
        for result in results:
            sid = result["session"]["id"]
            try:
                source_mtime, source_size = _local_file_fingerprint(result["session"]["raw_path"])
                should_sync, existing = _should_refresh_file_backed_session(
                    sid,
                    source_mtime,
                    source_size,
                    force=force,
                )
                if not should_sync:
                    stats["skipped"] += 1
                    continue
                metadata = result["session"].setdefault("metadata", {})
                metadata["source_file_mtime"] = source_mtime
                metadata["source_file_size"] = source_size
                upsert_session(result["session"])
                insert_messages(
                    sid, result["messages"],
                    force=(force or existing),
                    incremental=(existing and not force),
                )
                stats["synced"] += 1
            except Exception as e:
                logger.error(f"Error importing vscode memory {sid}: {e}")
                stats["errors"] += 1

        return stats

    def sync_vscode_chat_edits(self, force: bool = False) -> dict:
        """Download and import VS Code chat-edit history."""
        stats = {"synced": 0, "skipped": 0, "errors": 0}
        remote_vscode_dir = self._resolve_remote_path(self.server.vscode_dir)
        remote_history = f"{remote_vscode_dir}/data/User/History"
        try:
            stdin, stdout, stderr = self._exec_command(
                f"find {remote_history} -name 'entries.json' 2>/dev/null"
            )
            remote_files = [
                line.strip()
                for line in stdout.read().decode().splitlines()
                if line.strip()
            ]
        except Exception as exc:
            logger.warning(f"Cannot find vscode history files: {exc}")
            return stats
        if not remote_files:
            return stats

        temp_base = get_config().sync.temp_dir
        local_vscode_root = os.path.join(
            temp_base, self.server.name, "vscode_history_root"
        )
        for remote_file in remote_files:
            relative = remote_file
            if remote_vscode_dir in relative:
                relative = relative[len(remote_vscode_dir):]
            local_file = os.path.join(
                local_vscode_root, relative.lstrip("/")
            )
            try:
                remote_stat = self.sftp.stat(remote_file)
                remote_size = int(getattr(remote_stat, "st_size", 0) or 0)
                remote_mtime = int(getattr(remote_stat, "st_mtime", 0) or 0)
                cached = (
                    os.path.exists(local_file)
                    and os.path.getsize(local_file) == remote_size
                    and int(os.path.getmtime(local_file) or 0) == remote_mtime
                )
                if force or not cached:
                    self._download_file(remote_file, local_file)
            except Exception as exc:
                logger.warning(f"Cannot cache history file {remote_file}: {exc}")
                stats["errors"] += 1

        for result in parse_vscode_chat_edits(
            local_vscode_root, self.server.name
        ):
            sid = result["session"]["id"]
            existing, _metadata = _get_session_metadata(sid)
            if existing and not force:
                stats["skipped"] += 1
                continue
            try:
                upsert_session(result["session"])
                insert_messages(
                    sid,
                    result["messages"],
                    force=(force or existing),
                )
                stats["synced"] += 1
            except Exception as exc:
                logger.error(f"Error importing vscode chat edit {sid}: {exc}")
                stats["errors"] += 1
        return stats

    def sync_mac_chat_sessions(self, force: bool = False) -> dict:
        """Sync VS Code chatSessions from Mac desktop via SSH."""
        stats = {"synced": 0, "skipped": 0, "errors": 0, "downloaded": 0}

        # Find all chatSessions JSONL files on the Mac
        try:
            stdin, stdout, stderr = self._exec_command(
                "find ~/Library/Application\\ Support/Code/User/workspaceStorage "
                "-path '*/chatSessions/*.jsonl' -size +2k 2>/dev/null"
            )
            remote_files = [
                line.strip()
                for line in stdout.read().decode().splitlines()
                if line.strip()
            ]
        except Exception as e:
            logger.error(f"Cannot find Mac chatSessions: {e}")
            stats["errors"] += 1
            return stats

        if not remote_files:
            logger.info("No Mac chatSessions files found")
            return stats

        logger.info(f"Found {len(remote_files)} Mac chatSession files")

        # Local destination
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'data')
        local_sessions_dir = os.path.join(data_dir, 'mac_chat_sessions')
        os.makedirs(local_sessions_dir, exist_ok=True)

        # Download only new or changed files
        for remote_file in remote_files:
            filename = os.path.basename(remote_file)
            local_file = os.path.join(local_sessions_dir, filename)

            try:
                remote_stat = self.sftp.stat(remote_file)
                remote_size = remote_stat.st_size
                remote_mtime = int(getattr(remote_stat, "st_mtime", 0) or 0)

                # Skip only if local snapshot matches the remote file fingerprint.
                if os.path.exists(local_file) and not force:
                    local_size = os.path.getsize(local_file)
                    local_mtime = int(os.path.getmtime(local_file) or 0)
                    if local_size == remote_size and local_mtime == remote_mtime:
                        continue

                self._download_file(remote_file, local_file)
                stats["downloaded"] += 1
            except Exception as e:
                logger.warning(f"Cannot download Mac chatSession {filename}: {e}")
                stats["errors"] += 1

        # Also download the manifest file
        try:
            # Alternative: build manifest from workspace folder names
            stdin2, stdout2, stderr2 = self._exec_command(
                "ls -d ~/Library/Application\\ Support/Code/User/workspaceStorage/*/chatSessions/ "
                "2>/dev/null"
            )
            ws_dirs = [
                line.strip()
                for line in stdout2.read().decode().splitlines()
                if line.strip()
            ]

            # Build a manifest mapping session filenames to workspace hashes
            manifest = {}
            for ws_dir in ws_dirs:
                # Extract workspace hash from path
                parts = ws_dir.split('/workspaceStorage/')
                if len(parts) > 1:
                    ws_hash = parts[1].split('/')[0]
                    try:
                        session_files = self.sftp.listdir(ws_dir)
                        for sf in session_files:
                            if sf.endswith('.jsonl'):
                                session_id = sf.replace('.jsonl', '')
                                manifest[session_id] = {"workspace_hash": ws_hash}
                    except Exception:
                        pass

            manifest_file = os.path.join(data_dir, 'mac_chat_manifest.json')
            import json
            with open(manifest_file, 'w') as f:
                json.dump(manifest, f)
        except Exception as e:
            logger.warning(f"Could not build Mac chat manifest: {e}")

        if stats["downloaded"] > 0:
            logger.info(f"Downloaded {stats['downloaded']} new Mac chatSession files")

        # Parse and import
        mac_manifest = os.path.join(data_dir, 'mac_chat_manifest.json')
        try:
            all_snapshots = [
                os.path.join(local_sessions_dir, name)
                for name in os.listdir(local_sessions_dir)
                if name.endswith(".jsonl")
            ]
            changed_files, db_skipped = _filter_changed_mac_chat_files(
                all_snapshots, "mac", force
            )
            stats["skipped"] += db_skipped
            for result in iter_mac_chat_sessions(
                local_sessions_dir,
                mac_manifest,
                "mac",
                filepaths=changed_files,
            ):
                sid = result["session"]["id"]
                try:
                    source_mtime, source_size = _local_file_fingerprint(result["session"]["raw_path"])
                    should_sync, existing = _should_refresh_file_backed_session(
                        sid,
                        source_mtime,
                        source_size,
                        force=force,
                    )
                    if not should_sync:
                        stats["skipped"] += 1
                        continue
                    metadata = result["session"].setdefault("metadata", {})
                    metadata["source_file_mtime"] = source_mtime
                    metadata["source_file_size"] = source_size
                    upsert_session(result["session"])
                    insert_messages(
                        sid,
                        result["messages"],
                        force=(force or existing),
                        incremental=(existing and not force),
                    )
                    stats["synced"] += 1
                except Exception as e:
                    logger.error(f"Error importing Mac chat session {sid}: {e}")
                    stats["errors"] += 1
        except Exception as e:
            logger.warning(f"Error parsing Mac chat sessions: {e}")
            stats["errors"] += 1

        return stats

    def sync_all(self, force: bool = False) -> dict:
        total = {"copilot": {}, "codex": {}, "opencode": {}, "vscode_metadata": {},
                 "vscode_memories": {},
                 "vscode_chat_edits": {},
                 "claude": {}, "server": self.server.name}
        try:
            self.connect()
            if getattr(self.server, 'is_mac', False):
                # Mac desktop: sync chatSessions via SSH + CLI sessions
                total["mac_chat_sessions"] = self.sync_mac_chat_sessions(force=force)
                total["copilot"] = self.sync_copilot(force=force)
                total["codex"] = self.sync_codex(force=force)
                total["claude"] = self.sync_claude(force=force)
                total["opencode"] = self.sync_opencode(force=force)
            else:
                total["copilot"] = self.sync_copilot(force=force)
                total["codex"] = self.sync_codex(force=force)
                total["claude"] = self.sync_claude(force=force)
                total["opencode"] = self.sync_opencode(force=force)
                total["vscode_metadata"] = self.sync_vscode_metadata(force=force)
                total["vscode_memories"] = self.sync_vscode_memories(force=force)
                total["vscode_chat_edits"] = self.sync_vscode_chat_edits(
                    force=force
                )
        except Exception as e:
            logger.error(f"Sync error for {self.server.name}: {e}")
            total["error"] = str(e)
        finally:
            self.disconnect()
        return total

def sync_local(
    force: bool = False,
    device: Optional[ServerConfig] = None,
) -> dict:
    """Sync enabled local-device directories.

    ``device`` is supplied by an all-device snapshot so one run stays
    internally consistent. Direct/manual calls resolve the current persisted
    local profile. If that profile was deleted or disabled, no new local data
    is collected.
    """
    if device is None:
        device = next(
            (
                candidate
                for candidate in get_sync_server_configs(enabled_only=True)
                if candidate.is_local
            ),
            None,
        )
    stats = {"copilot": {"synced": 0, "skipped": 0, "errors": 0},
             "codex": {"synced": 0, "skipped": 0, "errors": 0},
             "claude": {"synced": 0, "skipped": 0, "errors": 0},
             "opencode": {"synced": 0, "skipped": 0, "errors": 0},
             "vscode_metadata": {"enriched": 0, "errors": 0},
             "vscode_memories": {"synced": 0, "skipped": 0, "errors": 0},
             "vscode_chat_edits": {"synced": 0, "skipped": 0, "errors": 0},
             "vscode_panel": {"synced": 0, "skipped": 0, "errors": 0},
             "vscode_debug_logs": {"synced": 0, "skipped": 0, "errors": 0},
             "mac_chat_sessions": {"synced": 0, "skipped": 0, "errors": 0}}

    if device is None:
        stats["skipped"] = True
        stats["reason"] = "未配置已启用的本机同步设备"
        return stats

    # Local copilot
    copilot_dir = os.path.expanduser(device.copilot_dir)
    if os.path.exists(copilot_dir):
        for session_id in os.listdir(copilot_dir):
            session_dir = os.path.join(copilot_dir, session_id)
            if not os.path.isdir(session_dir):
                continue

            # Try events.jsonl first (standard copilot session)
            events_file = os.path.join(session_dir, "events.jsonl")
            if os.path.exists(events_file):
                composite_id = f"copilot-local-{session_id}"
                file_stat = os.stat(events_file)
                source_mtime = int(file_stat.st_mtime or 0)
                source_size = int(file_stat.st_size or 0)
                should_sync, existing = _should_refresh_file_backed_session(
                    composite_id,
                    source_mtime,
                    source_size,
                    force=force,
                )
                if not should_sync:
                    stats["copilot"]["skipped"] += 1
                    continue
                try:
                    result = parse_copilot_session(session_dir, "local")
                    if result:
                        metadata = result["session"].setdefault("metadata", {})
                        metadata["source_file_mtime"] = source_mtime
                        metadata["source_file_size"] = source_size
                        upsert_session(result["session"])
                        insert_messages(
                            result["session"]["id"], result["messages"],
                            force=(force or existing),
                            incremental=(existing and not force),
                        )
                        stats["copilot"]["synced"] += 1
                except Exception as e:
                    logger.error(f"Error parsing local copilot session {session_id}: {e}")
                    stats["copilot"]["errors"] += 1
            else:
                # Try workspace.yaml (Copilot Coding Agent session)
                ws_file = os.path.join(session_dir, "workspace.yaml")
                if os.path.exists(ws_file):
                    composite_id = f"copilot-agent-local-{session_id}"
                    file_stat = os.stat(ws_file)
                    source_mtime = int(file_stat.st_mtime or 0)
                    source_size = int(file_stat.st_size or 0)
                    should_sync, existing = _should_refresh_file_backed_session(
                        composite_id,
                        source_mtime,
                        source_size,
                        force=force,
                    )
                    if not should_sync:
                        stats["copilot"]["skipped"] += 1
                        continue
                    try:
                        result = parse_copilot_coding_agent(session_dir, "local")
                        if result:
                            metadata = result["session"].setdefault("metadata", {})
                            metadata["source_file_mtime"] = source_mtime
                            metadata["source_file_size"] = source_size
                            upsert_session(result["session"])
                            insert_messages(
                                result["session"]["id"], result["messages"],
                                force=(force or existing),
                                incremental=(existing and not force),
                            )
                            stats["copilot"]["synced"] += 1
                    except Exception as e:
                        logger.error(f"Error parsing local coding agent session {session_id}: {e}")
                        stats["copilot"]["errors"] += 1

    # Local codex (with subagent merging)
    codex_dir = os.path.expanduser(device.codex_dir)
    if os.path.exists(codex_dir):
        index_entries = {}
        entries = parse_codex_index(codex_dir)
        index_entries = {e["id"]: e for e in entries}

        rollout_files = find_codex_rollout_files(codex_dir)
        _sync_codex_cached_files(
            [{"path": rollout_file} for rollout_file in rollout_files],
            "local",
            index_entries,
            force,
            stats["codex"],
        )

    # Local Claude project JSONL sessions
    claude_projects_dir = os.path.expanduser(_claude_projects_dir(device))
    if os.path.isdir(claude_projects_dir):
        for session_file in find_claude_session_files(claude_projects_dir):
            session_uuid = os.path.splitext(os.path.basename(session_file))[0]
            composite_id = f"claude-local-{session_uuid}"
            try:
                source_mtime, source_size = _local_file_fingerprint(session_file)
                should_sync, existing = _should_refresh_file_backed_session(
                    composite_id,
                    source_mtime,
                    source_size,
                    force=force,
                )
                if not should_sync:
                    stats["claude"]["skipped"] += 1
                    continue
                result = parse_claude_session(session_file, "local")
                if result:
                    metadata = result["session"].setdefault("metadata", {})
                    metadata["source_file_mtime"] = source_mtime
                    metadata["source_file_size"] = source_size
                    upsert_session(result["session"])
                    insert_messages(
                        result["session"]["id"], result["messages"],
                        force=(force or existing),
                        incremental=(existing and not force),
                    )
                    stats["claude"]["synced"] += 1
            except Exception as e:
                logger.error(f"Error importing local claude session {session_file}: {e}")
                stats["claude"]["errors"] += 1

    # Local OpenCode SQLite or legacy JSON storage
    opencode_dir = getattr(device, "opencode_dir", "") or ""
    try:
        source = scan_opencode_source(opencode_dir)
        _sync_opencode_source(source, "local", force, stats["opencode"])
    except Exception as exc:
        logger.error(f"Error importing local OpenCode sessions: {exc}")
        stats["opencode"]["errors"] += 1

    # Local VS Code: session metadata enrichment
    vscode_dir = os.path.expanduser(device.vscode_dir)
    if os.path.isdir(vscode_dir):
        try:
            metadata = parse_vscode_session_metadata(vscode_dir)
            for session_uuid, meta in metadata.items():
                try:
                    enrich_session_metadata(
                        session_uuid,
                        workspace_folder=meta.get("workspace_folder"),
                        first_user_message=meta.get("first_user_message"),
                    )
                    stats["vscode_metadata"]["enriched"] += 1
                except Exception as e:
                    logger.warning(f"Error enriching local session {session_uuid}: {e}")
                    stats["vscode_metadata"]["errors"] += 1
        except Exception as e:
            logger.warning(f"Error parsing local vscode metadata: {e}")
            stats["vscode_metadata"]["errors"] += 1

        # Local VS Code: memory files
        try:
            memory_results = parse_vscode_memory_files(vscode_dir, "local")
            for result in memory_results:
                sid = result["session"]["id"]
                try:
                    source_mtime, source_size = _local_file_fingerprint(result["session"]["raw_path"])
                    should_sync, existing = _should_refresh_file_backed_session(
                        sid,
                        source_mtime,
                        source_size,
                        force=force,
                    )
                    if not should_sync:
                        stats["vscode_memories"]["skipped"] += 1
                        continue
                    metadata = result["session"].setdefault("metadata", {})
                    metadata["source_file_mtime"] = source_mtime
                    metadata["source_file_size"] = source_size
                    upsert_session(result["session"])
                    insert_messages(
                        sid, result["messages"],
                        force=(force or existing),
                        incremental=(existing and not force),
                    )
                    stats["vscode_memories"]["synced"] += 1
                except Exception as e:
                    logger.error(f"Error importing local vscode memory {sid}: {e}")
                    stats["vscode_memories"]["errors"] += 1
        except Exception as e:
            logger.warning(f"Error parsing local vscode memories: {e}")
            stats["vscode_memories"]["errors"] += 1


    # Mac VS Code Chat Sessions (from client-side export)
    # Exported Mac chat snapshots are a local application cache, not a path on
    # the local SSH-style device profile, so this setting remains in YAML.
    configured_mac_dir = getattr(
        get_config().local, "mac_chat_sessions_dir", ""
    ) or ""
    mac_sessions_dir = (
        os.path.abspath(os.path.expanduser(configured_mac_dir))
        if configured_mac_dir else ""
    )
    mac_manifest = (
        os.path.join(os.path.dirname(mac_sessions_dir), "mac_chat_manifest.json")
        if mac_sessions_dir else ""
    )
    if mac_sessions_dir and os.path.isdir(mac_sessions_dir):
        try:
            all_snapshots = [
                os.path.join(mac_sessions_dir, name)
                for name in os.listdir(mac_sessions_dir)
                if name.endswith(".jsonl")
            ]
            changed_files, skipped = _filter_changed_mac_chat_files(
                all_snapshots, "mac", force
            )
            stats["mac_chat_sessions"]["skipped"] += skipped
            for result in iter_mac_chat_sessions(
                mac_sessions_dir,
                mac_manifest,
                "mac",
                filepaths=changed_files,
            ):
                sid = result["session"]["id"]
                try:
                    source_mtime, source_size = _local_file_fingerprint(result["session"]["raw_path"])
                    should_sync, existing = _should_refresh_file_backed_session(
                        sid,
                        source_mtime,
                        source_size,
                        force=force,
                    )
                    if not should_sync:
                        stats["mac_chat_sessions"]["skipped"] += 1
                        continue
                    metadata = result["session"].setdefault("metadata", {})
                    metadata["source_file_mtime"] = source_mtime
                    metadata["source_file_size"] = source_size
                    upsert_session(result["session"])
                    insert_messages(
                        sid,
                        result["messages"],
                        force=(force or existing),
                        incremental=(existing and not force),
                    )
                    stats["mac_chat_sessions"]["synced"] += 1
                except Exception as e:
                    logger.error(f"Error importing Mac chat session {sid}: {e}")
                    stats["mac_chat_sessions"]["errors"] += 1
        except Exception as e:
            logger.warning(f"Error parsing Mac chat sessions: {e}")
            stats["mac_chat_sessions"]["errors"] += 1

    return stats

def sync_all_servers(force: bool = False) -> List[dict]:
    """Sync from all configured servers + local."""
    results = []
    servers = list(get_sync_server_configs(enabled_only=True))

    # Local first when an enabled local device profile exists. Deleting or
    # disabling that profile stops future automatic collection while retaining
    # all previously imported local data.
    if any(server.is_local for server in servers):
        local_device = next(server for server in servers if server.is_local)
        local_result = sync_local(force=force, device=local_device)
        local_result["server"] = "local"
        results.append(local_result)

    # Remote servers (skip those marked is_local to avoid duplicates)
    for server in servers:
        if server.is_local:
            continue
        syncer = SSHSyncer(server)
        result = syncer.sync_all(force=force)
        results.append(result)

    return results
