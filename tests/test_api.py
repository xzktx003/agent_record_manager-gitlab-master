"""Integration tests for the API endpoints."""
import json
import os
import tempfile
import unittest

class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Set up temp database
        cls.tmpdir = tempfile.mkdtemp()
        os.environ["AM_CONFIG"] = ""  # Use defaults
        import app.database as db
        db._db_path = os.path.join(cls.tmpdir, "test_api.db")
        db.init_db()

        # Insert test data
        db.upsert_session({
            "id": "api-test-1", "source": "copilot", "server_name": "local",
            "title": "API Test Session", "cwd": "/tmp",
            "start_time": "2026-04-08T10:00:00Z", "message_count": 2,
        })
        db.insert_messages("api-test-1", [
            {"role": "user", "content": "Hello API test",
             "msg_type": "user.message", "timestamp": "2026-04-08T10:00:01Z"},
            {"role": "assistant", "content": "API response here",
             "msg_type": "assistant.message", "timestamp": "2026-04-08T10:00:02Z"},
        ])
        db.upsert_session({
            "id": "api-long-1", "source": "codex", "server_name": "local",
            "title": "Long API Session", "message_count": 1,
        })
        db.insert_messages("api-long-1", [{
            "role": "assistant",
            "content": "begin-" + ("z" * 10000) + "-end",
            "msg_type": "agent_message",
        }])

        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

        live = self.client.get("/health/live")
        self.assertEqual(live.status_code, 200)
        self.assertIn("uptime_seconds", live.json())
        self.assertIn("server-timing", live.headers)

        ready = self.client.get("/health/ready")
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertEqual(ready.json()["database"]["status"], "ok")

        runtime = self.client.get("/api/diagnostics/runtime")
        self.assertEqual(runtime.status_code, 200)
        self.assertIn("thread_pool", runtime.json())
        self.assertIn("http", runtime.json())

        metrics = self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("agent_memory_http_in_flight", metrics.text)

    def test_readiness_rejects_a_saturated_request_pool(self):
        from unittest.mock import patch

        saturated = {
            "borrowed_tokens": 40,
            "total_tokens": 40,
            "available_tokens": 0,
            "saturated": True,
        }
        with patch("app.reliability._thread_pool_snapshot", return_value=saturated):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")

    def test_readiness_rejects_dead_background_worker(self):
        from unittest.mock import patch

        unhealthy = {
            "status": "error",
            "agent": {
                "status": "dead",
                "worker_alive": False,
                "paused": False,
                "restart_count": 1,
            },
            "sync": {
                "status": "ok",
                "running": False,
                "running_for_s": 0,
                "stale_after_s": 1800,
            },
        }
        with patch("app.reliability._background_readiness", return_value=unhealthy):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["background"]["agent"]["status"], "dead")

    def test_list_sessions(self):
        r = self.client.get("/api/sessions")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("sessions", data)
        self.assertGreaterEqual(data["total"], 1)

    def test_session_detail(self):
        r = self.client.get("/api/sessions/api-test-1")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["title"], "API Test Session")
        self.assertEqual(len(data["messages"]), 2)

        assistant = self.client.get(
            "/api/sessions/api-test-1?message_view=assistant"
        )
        self.assertEqual(assistant.status_code, 200)
        self.assertEqual(
            [message["content"] for message in assistant.json()["messages"]],
            ["API response here"],
        )
        self.assertEqual(assistant.json()["msg_total_unfiltered"], 2)
        self.assertEqual(
            self.client.get(
                "/api/sessions/api-test-1?message_view=unknown"
            ).status_code,
            422,
        )

    def test_session_detail_all_messages(self):
        r = self.client.get("/api/sessions/api-test-1?msg_limit=0")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data["messages"]), 2)
        self.assertFalse(data["msg_has_more"])

    def test_session_detail_uses_preview_and_full_message_endpoint(self):
        page = self.client.get(
            "/api/sessions/api-long-1?msg_limit=100&content_limit=64"
        )
        self.assertEqual(page.status_code, 200)
        message = page.json()["messages"][0]
        self.assertEqual(len(message["content"]), 64)
        self.assertTrue(message["content_truncated"])

        full = self.client.get(
            f"/api/sessions/api-long-1/messages/{message['id']}"
        )
        self.assertEqual(full.status_code, 200)
        self.assertTrue(full.json()["content"].endswith("-end"))

        mcp = self.client.get("/api/mcp/session/api-long-1")
        self.assertEqual(mcp.status_code, 200)
        mcp_data = mcp.json()
        self.assertEqual(mcp_data["message_page"]["total"], 1)
        self.assertTrue(mcp_data["messages"][0]["content_truncated"])
        self.assertEqual(len(mcp_data["messages"][0]["content"]), 8000)

    def test_search(self):
        r = self.client.get("/api/search?q=API+test")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-store")
        data = r.json()
        self.assertGreater(len(data["results"]), 0)
        self.assertEqual(data["result_unit"], "session")
        self.assertGreaterEqual(data["total"], data["count"])

        grouped = self.client.get("/api/search?q=API").json()
        session_ids = [row["session_id"] for row in grouped["results"]]
        self.assertEqual(len(session_ids), len(set(session_ids)))
        self.assertEqual(grouped["total"], 2)
        api_test = next(
            row for row in grouped["results"]
            if row["session_id"] == "api-test-1"
        )
        self.assertEqual(api_test["match_count"], 2)

        short = self.client.get("/api/search?q=zz")
        self.assertEqual(short.status_code, 200)
        self.assertEqual(short.json()["scope"], "session_metadata")
        self.assertEqual(short.json()["count"], 0)

        wildcard_only = self.client.get(
            "/api/search",
            params={"q": "%%%"},
        )
        self.assertEqual(
            wildcard_only.json()["scope"],
            "session_metadata",
        )

    def test_frontend_is_not_cached(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("wm-today-last-good", r.text)
        self.assertIn("pipelineRefreshPromise", r.text)
        self.assertIn("todayContinuityState", r.text)

    def test_stats(self):
        r = self.client.get("/api/sessions/stats")
        self.assertEqual(r.status_code, 200)
        self.assertIn("total_sessions", r.json())

    def test_export_json(self):
        r = self.client.get("/api/export/session/api-test-1?format=json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["messages"]), 2)

        long_export = self.client.get(
            "/api/export/session/api-long-1?format=json"
        )
        self.assertTrue(
            long_export.json()["messages"][0]["content"].endswith("-end")
        )

    def test_export_markdown(self):
        r = self.client.get("/api/export/session/api-test-1?format=markdown")
        self.assertEqual(r.status_code, 200)
        self.assertIn("API Test Session", r.text)

    def test_export_all_stream_formats_are_valid(self):
        all_json = self.client.get("/api/export/all?format=json")
        self.assertEqual(all_json.status_code, 200)
        self.assertGreaterEqual(len(all_json.json()), 2)

        jsonl = self.client.get("/api/export/all?format=openai_jsonl")
        self.assertEqual(jsonl.status_code, 200)
        records = [
            json.loads(line)
            for line in jsonl.text.splitlines()
            if line.strip()
        ]
        self.assertTrue(records)
        self.assertTrue(all("messages" in record for record in records))

    def test_mcp_search(self):
        r = self.client.get("/api/mcp/search?q=API")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("results", data)

    def test_mcp_summary(self):
        r = self.client.get("/api/mcp/summary")
        self.assertEqual(r.status_code, 200)
        self.assertIn("total_sessions", r.json())


class TestResumeCommand(unittest.TestCase):
    """Tests for build_resume_command + resume_command field in session detail."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        import app.database as db
        db._db_path = os.path.join(cls.tmpdir, "test_resume.db")
        db.init_db()
        # codex session with cwd
        db.upsert_session({
            "id": "codex-local-10a222c1-9599-48e0-8549-29b12a34511c",
            "source": "codex", "server_name": "local",
            "title": "codex resume test", "cwd": "/tmp/codex proj",
            "start_time": "2026-04-08T10:00:00Z", "message_count": 0,
        })
        # claude session with cwd
        db.upsert_session({
            "id": "claude-local-22b333d2-1111-2222-3333-44455566aaaa",
            "source": "claude", "server_name": "local",
            "title": "claude resume test", "cwd": "/home/user/work",
            "start_time": "2026-04-08T10:00:00Z", "message_count": 0,
        })
        # vscode-chat session (unsupported)
        db.upsert_session({
            "id": "vscode-chat-local-abcdef12-3456-7890-abcd-ef1234567890",
            "source": "vscode-chat", "server_name": "local",
            "title": "vscode chat test", "cwd": "/tmp",
            "start_time": "2026-04-08T10:00:00Z", "message_count": 0,
        })
        db.upsert_session({
            "id": "opencode-local-ses_abc123XYZ",
            "source": "opencode", "server_name": "local",
            "title": "opencode resume test", "cwd": "/tmp/open code",
            "start_time": "2026-08-07T10:00:00Z", "message_count": 0,
        })
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_codex_resume_command(self):
        r = self.client.get("/api/sessions/codex-local-10a222c1-9599-48e0-8549-29b12a34511c")
        self.assertEqual(r.status_code, 200)
        cmd = r.json().get("resume_command")
        self.assertIsNotNone(cmd)
        self.assertIn("codex resume", cmd)
        self.assertIn("10a222c1-9599-48e0-8549-29b12a34511c", cmd)

    def test_claude_resume_command_has_cd_prefix(self):
        import shlex
        r = self.client.get("/api/sessions/claude-local-22b333d2-1111-2222-3333-44455566aaaa")
        self.assertEqual(r.status_code, 200)
        cmd = r.json().get("resume_command")
        self.assertIsNotNone(cmd)
        self.assertTrue(
            cmd.startswith(f"cd {shlex.quote('/home/user/work')} && "),
            f"unexpected cmd: {cmd}",
        )
        self.assertIn("claude --resume 22b333d2-1111-2222-3333-44455566aaaa", cmd)

    def test_vscode_chat_resume_command_is_none(self):
        r = self.client.get("/api/sessions/vscode-chat-local-abcdef12-3456-7890-abcd-ef1234567890")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json().get("resume_command"))

    def test_opencode_resume_command_supports_non_uuid_session_id(self):
        import shlex

        r = self.client.get("/api/sessions/opencode-local-ses_abc123XYZ")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.json().get("resume_command"),
            f"cd {shlex.quote('/tmp/open code')} && opencode --session ses_abc123XYZ",
        )

    def test_build_resume_command_no_cwd(self):
        from app.database import build_resume_command
        cmd = build_resume_command(
            "codex",
            "codex-local-10a222c1-9599-48e0-8549-29b12a34511c",
            None,
        )
        self.assertEqual(cmd, "codex resume 10a222c1-9599-48e0-8549-29b12a34511c")

    def test_build_resume_command_bad_uuid(self):
        from app.database import build_resume_command
        self.assertIsNone(build_resume_command("codex", "codex-local-not-a-uuid", "/tmp"))


class TestSyncAPI(unittest.TestCase):
    """POST endpoints must return immediately and /status reflects real state."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        import app.database as db
        db._db_path = os.path.join(cls.tmpdir, "test_sync_api.db")
        db.init_db()
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _reset_manager(self):
        from app.scheduler import sync_manager
        # Wait for any prior job to drain.
        sync_manager.wait(timeout=10)

    def test_post_local_returns_immediately(self):
        import time
        import threading
        from unittest.mock import patch
        from app import scheduler as sched_mod

        self._reset_manager()
        release = threading.Event()

        def slow_local(force=False):
            release.wait(timeout=5)
            return {"claude": {"synced": 0, "skipped": 0, "errors": 0}}

        with patch.object(sched_mod, "sync_local", slow_local):
            t0 = time.time()
            r = self.client.post("/api/sync/local")
            elapsed = time.time() - t0
            self.assertLess(elapsed, 1.0, "POST /api/sync/local must return < 1s")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body.get("status"), "started")
            self.assertIn("job_id", body)

            # /status reports running=True
            r2 = self.client.get("/api/sync/status")
            self.assertEqual(r2.status_code, 200)
            self.assertTrue(r2.json().get("running"))

            # Second POST gets already_running
            r3 = self.client.post("/api/sync/local")
            self.assertEqual(r3.json().get("status"), "already_running")

            release.set()
            sched_mod.sync_manager.wait(timeout=5)

        final = self.client.get("/api/sync/status").json()
        self.assertFalse(final["running"])
        self.assertEqual(final["last_status"], "success")

    def test_post_now_and_force_reimport_nonblocking(self):
        import time
        from unittest.mock import patch
        from app import scheduler as sched_mod

        self._reset_manager()
        with patch.object(sched_mod, "sync_local", return_value={
            "claude": {"synced": 1, "skipped": 0, "errors": 0}}), \
             patch.object(sched_mod, "sync_all_servers", return_value=[
                 {"server": "local", "claude": {"synced": 1, "skipped": 0, "errors": 0}}
             ]):
            t0 = time.time()
            r = self.client.post("/api/sync/now")
            self.assertLess(time.time() - t0, 1.0)
            self.assertEqual(r.json().get("status"), "started")
            sched_mod.sync_manager.wait(timeout=5)

            t0 = time.time()
            r2 = self.client.post("/api/sync/force-reimport")
            self.assertLess(time.time() - t0, 1.0)
            self.assertIn(r2.json().get("status"), ("started", "already_running"))
            sched_mod.sync_manager.wait(timeout=5)

    def test_status_endpoint_shape(self):
        self._reset_manager()
        r = self.client.get("/api/sync/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("running", "started_at", "last_finished_at",
                    "last_result", "last_error", "last_status"):
            self.assertIn(key, body)


if __name__ == "__main__":
    unittest.main()
