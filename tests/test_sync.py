"""Unit tests for sync import paths."""
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import stat
import subprocess
from unittest.mock import patch


class TestSync(unittest.TestCase):
    def setUp(self):
        import app.config as config
        import app.database as db

        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_sync.db")
        db._db_path = self.db_path
        db.init_db()
        config._config = config.AppConfig(
            local=config.LocalConfig(
                copilot_dir=os.path.join(self.tmpdir.name, "missing-copilot"),
                codex_dir=os.path.join(self.tmpdir.name, "missing-codex"),
                vscode_dir=os.path.join(self.tmpdir.name, "missing-vscode"),
                claude_projects_dir=os.path.join(self.tmpdir.name, "claude", "projects"),
                opencode_dir=os.path.join(self.tmpdir.name, "missing-opencode"),
            )
        )

    def tearDown(self):
        import app.config as config
        config._config = None
        self.tmpdir.cleanup()

    def test_sync_local_imports_claude_projects(self):
        from app.database import get_session_detail
        from app.sync.ssh_sync import sync_local

        project_dir = os.path.join(self.tmpdir.name, "claude", "projects", "project-a")
        os.makedirs(project_dir)
        session_file = os.path.join(project_dir, "session-abc.jsonl")
        events = [
            {"type": "user", "message": {"role": "user", "content": "sync me"},
             "timestamp": "2026-05-13T10:00:00Z", "cwd": "/tmp/project-a"},
            {"type": "assistant", "message": {"role": "assistant", "content": "synced"},
             "timestamp": "2026-05-13T10:00:01Z"},
        ]
        with open(session_file, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        result = sync_local(force=True)

        self.assertIn("claude", result)
        self.assertEqual(result["claude"]["synced"], 1)
        detail = get_session_detail("claude-local-session-abc")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["source"], "claude")
        self.assertEqual(detail["session_tag"], "CLD-ALSESSIONABC")
        self.assertEqual(len(detail["messages"]), 2)

    def test_sync_local_uses_claude_dir_projects_when_projects_dir_missing(self):
        import app.config as config
        from app.sync.ssh_sync import sync_local

        claude_dir = os.path.join(self.tmpdir.name, "custom-claude")
        config._config = config.AppConfig(
            local=config.LocalConfig(
                copilot_dir=os.path.join(self.tmpdir.name, "missing-copilot"),
                codex_dir=os.path.join(self.tmpdir.name, "missing-codex"),
                vscode_dir=os.path.join(self.tmpdir.name, "missing-vscode"),
                claude_dir=claude_dir,
                opencode_dir=os.path.join(self.tmpdir.name, "missing-opencode"),
            )
        )
        project_dir = os.path.join(claude_dir, "projects", "project-b")
        os.makedirs(project_dir)
        session_file = os.path.join(project_dir, "session-def.jsonl")
        with open(session_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "fallback sync"},
                "timestamp": "2026-05-13T11:00:00Z",
            }) + "\n")

        result = sync_local(force=True)

        self.assertEqual(result["claude"]["synced"], 1)

    def test_codex_source_signature_includes_parser_version(self):
        from app.sync.ssh_sync import _build_source_signature

        source_files = [{"path": "/tmp/rollout.jsonl", "mtime": 123, "size": 456}]
        old_raw = json.dumps(source_files, ensure_ascii=False, separators=(",", ":"))
        old_signature = hashlib.sha1(old_raw.encode("utf-8")).hexdigest()

        self.assertNotEqual(_build_source_signature(source_files), old_signature)

    def test_remote_sync_all_includes_claude_stats(self):
        from app.config import ServerConfig
        from app.sync.ssh_sync import SSHSyncer

        syncer = SSHSyncer(ServerConfig(name="remote", host="example.invalid"))
        with patch.object(syncer, "connect"), \
             patch.object(syncer, "disconnect"), \
             patch.object(syncer, "sync_copilot", return_value={"synced": 0, "skipped": 0, "errors": 0}), \
             patch.object(syncer, "sync_codex", return_value={"synced": 0, "skipped": 0, "errors": 0}), \
             patch.object(syncer, "sync_claude", return_value={"synced": 2, "skipped": 0, "errors": 0}), \
             patch.object(syncer, "sync_opencode", return_value={"synced": 3, "skipped": 0, "errors": 0}), \
             patch.object(syncer, "sync_vscode_metadata", return_value={"enriched": 0, "errors": 0}), \
             patch.object(syncer, "sync_vscode_memories", return_value={"synced": 0, "skipped": 0, "errors": 0}), \
             patch.object(syncer, "sync_vscode_chat_edits", return_value={"synced": 0, "skipped": 0, "errors": 0}):
            result = syncer.sync_all(force=True)

        self.assertEqual(result["claude"], {"synced": 2, "skipped": 0, "errors": 0})
        self.assertEqual(result["opencode"], {"synced": 3, "skipped": 0, "errors": 0})

    def test_sync_local_replaces_changed_opencode_tool_state(self):
        import sqlite3
        import app.config as config
        from app.database import get_session_detail
        from app.sync.ssh_sync import sync_local

        opencode_dir = os.path.join(self.tmpdir.name, "opencode")
        os.makedirs(opencode_dir)
        database = os.path.join(opencode_dir, "opencode.db")
        conn = sqlite3.connect(database)
        conn.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
                slug TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL,
                version TEXT NOT NULL, time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL, data TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?)",
            ("ses_sync", "project", None, "sync", "/tmp/sync", "Sync OpenCode", "1.18", 1000, 3000),
        )
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            ("msg_sync", "ses_sync", 2000, 3000, json.dumps({
                "role": "assistant", "providerID": "test", "modelID": "model",
            })),
        )
        running = {
            "type": "tool", "tool": "bash", "callID": "call-sync",
            "state": {"status": "running", "input": {"command": "pytest"}, "time": {"start": 2100}},
        }
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            ("prt_sync", "msg_sync", "ses_sync", 2100, 3000, json.dumps(running)),
        )
        conn.commit()

        config._config.local.opencode_dir = opencode_dir
        first = sync_local(force=False)
        self.assertEqual(first["opencode"]["synced"], 1)
        detail = get_session_detail("opencode-local-ses_sync")
        self.assertFalse(any(m["msg_type"] == "tool_result" for m in detail["messages"]))

        completed = dict(running)
        completed["state"] = {
            "status": "completed", "input": {"command": "pytest"},
            "output": "all green", "time": {"start": 2100, "end": 4000},
        }
        conn.execute(
            "UPDATE part SET time_updated=?, data=? WHERE id='prt_sync'",
            (4000, json.dumps(completed)),
        )
        conn.execute(
            "UPDATE message SET time_updated=4000 WHERE id='msg_sync'"
        )
        conn.execute(
            "UPDATE session SET time_updated=4000 WHERE id='ses_sync'"
        )
        conn.commit()
        conn.close()

        second = sync_local(force=False)
        self.assertEqual(second["opencode"]["synced"], 1)
        detail = get_session_detail("opencode-local-ses_sync")
        result_messages = [
            m for m in detail["messages"] if m["msg_type"] == "tool_result"
        ]
        self.assertEqual(len(result_messages), 1)
        self.assertIn("all green", result_messages[0]["content"])

    def test_open_code_refresh_preserves_message_evidence_link(self):
        from app.database import get_db, replace_session_with_messages
        from app.sync.ssh_sync import _sync_opencode_source

        session = {
            "id": "opencode-local-ses_evidence",
            "source": "opencode",
            "server_name": "local",
            "title": "Evidence",
            "message_count": 1,
            "metadata": {"opencode_session_id": "ses_evidence"},
        }
        messages = [{
            "role": "user", "content": "before", "msg_type": "input_text",
            "metadata": {
                "opencode_message_id": "msg-evidence",
                "opencode_part_id": "prt-evidence",
            },
        }]
        replace_session_with_messages(session, messages)
        with get_db() as conn:
            message_id = conn.execute(
                "SELECT id FROM messages WHERE session_id=?", (session["id"],)
            ).fetchone()[0]
            conn.execute("""
                INSERT INTO work_items(
                    id,dedupe_key,kind,title,status,priority,confidence,
                    review_state,ai_generated)
                VALUES ('wi-evidence','key','task','Evidence','open','normal',1,'approved',0)
            """)
            conn.execute("""
                INSERT INTO work_item_evidence(
                    evidence_key,work_item_id,session_id,message_id,excerpt)
                VALUES ('ev-evidence','wi-evidence',?,?,?)
            """, (session["id"], message_id, "before"))

        source = {
            "kind": "sqlite",
            "path": "/tmp/not-read-by-mock.db",
            "schema_fingerprint": "schema",
            "groups": [{
                "session_id": "ses_evidence",
                "member_ids": ["ses_evidence"],
                "source_signature": "new-signature",
            }],
        }
        refreshed = {
            "session": dict(session),
            "messages": [dict(messages[0], content="after")],
        }
        with patch(
            "app.sync.ssh_sync.parse_opencode_groups",
            return_value=[refreshed],
        ):
            stats = {"synced": 0, "skipped": 0, "errors": 0}
            _sync_opencode_source(source, "local", False, stats)
        self.assertEqual(stats["synced"], 1)
        with get_db() as conn:
            row = conn.execute("""
                SELECT e.message_id,m.content FROM work_item_evidence e
                LEFT JOIN messages m ON m.id=e.message_id
                WHERE e.evidence_key='ev-evidence'
            """).fetchone()
        self.assertIsNotNone(row["message_id"])
        self.assertEqual(row["content"], "after")

    def test_sanitized_snapshot_excludes_credentials_and_is_private(self):
        from app.sync.ssh_sync import _OPENCODE_SNAPSHOT_SCRIPT

        source = os.path.join(self.tmpdir.name, "source.db")
        snapshot = os.path.join(self.tmpdir.name, "snapshot.db")
        conn = sqlite3.connect(source)
        conn.executescript("""
            CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL);
            CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL,
                title TEXT NOT NULL, time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                data TEXT NOT NULL);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE credential (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO credential VALUES ('secret','do-not-copy');
        """)
        conn.commit()
        conn.close()
        subprocess.run(
            ["python3", "-c", _OPENCODE_SNAPSHOT_SCRIPT, source, snapshot],
            check=True,
        )
        self.assertEqual(stat.S_IMODE(os.stat(snapshot).st_mode), 0o600)
        conn = sqlite3.connect(snapshot)
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()
        self.assertEqual(tables, {"project", "session", "message", "part"})

    def test_remote_fingerprint_cache_requires_all_session_ids(self):
        from app.database import upsert_session
        from app.sync.ssh_sync import _source_sessions_exist

        session_ids = [
            "opencode-remote-ses_a",
            "opencode-remote-ses_b",
        ]
        self.assertFalse(_source_sessions_exist(session_ids))
        for session_id in session_ids:
            upsert_session({
                "id": session_id,
                "source": "opencode",
                "server_name": "remote",
            })
        self.assertTrue(_source_sessions_exist(session_ids))

    def test_sync_local_honors_xdg_opencode_path(self):
        import app.config as config
        from app.sync.ssh_sync import sync_local

        data_home = os.path.join(self.tmpdir.name, "xdg")
        opencode_dir = os.path.join(data_home, "opencode")
        os.makedirs(opencode_dir)
        conn = sqlite3.connect(os.path.join(opencode_dir, "opencode.db"))
        conn.executescript("""
            CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL,
                title TEXT NOT NULL, time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                data TEXT NOT NULL);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL, data TEXT NOT NULL);
            INSERT INTO session VALUES ('ses_xdg','/tmp','XDG',1000,1000);
        """)
        conn.commit()
        conn.close()
        config._config.local.opencode_dir = ""
        with patch.dict(os.environ, {"XDG_DATA_HOME": data_home}, clear=False):
            result = sync_local(force=False)
        self.assertEqual(result["opencode"]["synced"], 1)


class TestSyncManager(unittest.TestCase):
    """SyncManager: non-blocking submit, lock, status, sync_log fan-out."""

    def setUp(self):
        import app.config as config
        import app.database as db
        from app.scheduler import SyncManager

        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_mgr.db")
        db._db_path = self.db_path
        db.init_db()
        config._config = config.AppConfig()
        self.manager = SyncManager()

    def tearDown(self):
        import app.config as config
        config._config = None
        self.tmpdir.cleanup()

    def test_submit_returns_immediately_and_rejects_concurrent(self):
        import threading
        import time
        from app import scheduler as sched_mod

        gate = threading.Event()
        release = threading.Event()
        call_count = {"n": 0}

        def slow_local(force=False):
            call_count["n"] += 1
            gate.set()
            release.wait(timeout=5)
            return {"claude": {"synced": 3, "skipped": 0, "errors": 0}}

        with patch.object(sched_mod, "sync_local", slow_local):
            t0 = time.time()
            first = self.manager.submit("local")
            elapsed = time.time() - t0
            self.assertLess(elapsed, 0.5, "submit() must return without blocking")
            self.assertEqual(first["status"], "started")
            self.assertIn("job_id", first)

            gate.wait(timeout=2)
            second = self.manager.submit("local")
            self.assertEqual(second["status"], "already_running")
            self.assertEqual(second["job_id"], first["job_id"])

            release.set()
            self.assertTrue(self.manager.wait(timeout=5))

        self.assertEqual(call_count["n"], 1, "concurrent submit must not double-run")
        status = self.manager.status()
        self.assertFalse(status["running"])
        self.assertEqual(status["last_status"], "success")
        self.assertEqual(status["last_result"], {"claude": {"synced": 3, "skipped": 0, "errors": 0}})

    def test_sync_log_covers_all_sources(self):
        from app import scheduler as sched_mod
        from app.database import get_db

        full_result = {
            "copilot": {"synced": 1, "skipped": 0, "errors": 0},
            "codex": {"synced": 0, "skipped": 2, "errors": 0},
            "claude": {"synced": 5, "skipped": 0, "errors": 0},
            "opencode": {"synced": 2, "skipped": 0, "errors": 0},
            "vscode_metadata": {"enriched": 4, "errors": 0},
            "vscode_memories": {"synced": 0, "skipped": 1, "errors": 0},
            "vscode_chat_edits": {"synced": 0, "skipped": 0, "errors": 0},
            "vscode_panel": {"synced": 0, "skipped": 0, "errors": 0},
            "vscode_debug_logs": {"synced": 0, "skipped": 0, "errors": 0},
            "mac_chat_sessions": {"synced": 0, "skipped": 0, "errors": 0},
        }
        with patch.object(sched_mod, "sync_local", return_value=full_result):
            outcome = self.manager.submit("local")
            self.assertEqual(outcome["status"], "started")
            self.assertTrue(self.manager.wait(timeout=5))

        with get_db() as conn:
            rows = conn.execute(
                "SELECT source FROM sync_log ORDER BY source"
            ).fetchall()
        sources = {r["source"] for r in rows}
        # Every known source must be represented, not just copilot/codex.
        for expected in (
            "copilot", "codex", "claude", "opencode", "vscode_metadata",
            "vscode_memories", "vscode_chat_edits", "vscode_panel",
            "vscode_debug_logs", "mac_chat_sessions",
        ):
            self.assertIn(expected, sources, f"missing sync_log row for {expected}")

    def test_failure_records_error_and_clears_running(self):
        from app import scheduler as sched_mod
        from app.database import get_db

        def boom(force=False):
            raise RuntimeError("boom")

        with patch.object(sched_mod, "sync_local", boom):
            self.manager.submit("local")
            self.assertTrue(self.manager.wait(timeout=5))

        status = self.manager.status()
        self.assertFalse(status["running"])
        self.assertEqual(status["last_status"], "error")
        self.assertIn("boom", status["last_error"])
        with get_db() as conn:
            rows = conn.execute(
                "SELECT status, error_message FROM sync_log"
            ).fetchall()
        self.assertTrue(any(r["status"] == "error" for r in rows))

    def test_device_failure_is_not_reported_as_success(self):
        from app import scheduler as sched_mod
        from app.database import get_db

        result = [
            {
                "server": "local",
                "codex": {"synced": 1, "skipped": 2, "errors": 0},
            },
            {
                "server": "Mac-Local",
                "codex": {},
                "error": "timed out",
            },
        ]
        with patch.object(sched_mod, "sync_all_servers", return_value=result):
            self.manager.submit("all")
            self.assertTrue(self.manager.wait(timeout=5))

        status = self.manager.status()
        self.assertEqual(status["last_status"], "error")
        self.assertIn("Mac-Local: timed out", status["last_error"])
        with get_db() as conn:
            local = conn.execute(
                """SELECT status FROM sync_log
                   WHERE server_name='local' AND source='codex'"""
            ).fetchone()
            remote = conn.execute(
                """SELECT status,error_message FROM sync_log
                   WHERE server_name='Mac-Local' AND source='all'"""
            ).fetchone()
        self.assertEqual(local["status"], "success")
        self.assertEqual(remote["status"], "error")
        self.assertIn("timed out", remote["error_message"])


if __name__ == "__main__":
    unittest.main()
