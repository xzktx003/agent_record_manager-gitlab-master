"""Authentication, CSRF, bearer-token, and tenant-isolation regressions."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest


class TestAuthenticationAndIsolation(unittest.TestCase):
    def setUp(self):
        import app.auth as auth
        import app.config as config
        import app.database as database
        import app.model_provider as model_provider
        import app.sync.device_store as device_store
        import app.tenant_data as tenant_data
        from app.enrichment import enrichment_manager
        from app.scheduler import sync_manager

        self.tmpdir = tempfile.mkdtemp()
        self.old_config = config._config
        self.old_db_path = database._db_path
        self.old_auth_db = os.environ.get("ARM_AUTH_DB")
        self.old_device_db = os.environ.get("ARM_SYNC_DEVICE_DB")
        self.old_provider_db = os.environ.get("ARM_MODEL_PROVIDER_DB")

        os.environ["ARM_AUTH_DB"] = os.path.join(self.tmpdir, "auth.db")
        os.environ["ARM_SYNC_DEVICE_DB"] = os.path.join(
            self.tmpdir, "devices.db"
        )
        os.environ["ARM_MODEL_PROVIDER_DB"] = os.path.join(
            self.tmpdir, "providers.db"
        )
        database._db_path = os.path.join(self.tmpdir, "sessions.db")
        config._config = config.AppConfig(
            auth=config.AuthConfig(enabled=True, session_hours=12),
            sync=config.SyncConfig(auto_start=False),
            agent=config.AgentConfig(enabled=False, auto_start_worker=False),
        )
        auth._schema_path = None
        device_store._schema_path = None
        model_provider._service = None
        tenant_data._initialized_paths.clear()
        enrichment_manager._managers.clear()
        sync_manager._managers.clear()

        auth.initialize_auth()
        self.admin_password = "Admin-test-password-2026"
        self.admin = auth.auth_repository.create_user(
            "admin",
            self.admin_password,
            is_admin=True,
            use_legacy_data=True,
        )

        from fastapi.testclient import TestClient
        from app.main import app

        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        import app.auth as auth
        import app.config as config
        import app.database as database
        import app.model_provider as model_provider
        import app.sync.device_store as device_store
        import app.tenant_data as tenant_data
        from app.enrichment import enrichment_manager
        from app.scheduler import sync_manager

        self.client.close()
        enrichment_manager.stop_all(timeout=1)
        auth._schema_path = None
        device_store._schema_path = None
        model_provider._service = None
        tenant_data._initialized_paths.clear()
        enrichment_manager._managers.clear()
        sync_manager._managers.clear()
        config._config = self.old_config
        database._db_path = self.old_db_path
        self._restore_env("ARM_AUTH_DB", self.old_auth_db)
        self._restore_env("ARM_SYNC_DEVICE_DB", self.old_device_db)
        self._restore_env("ARM_MODEL_PROVIDER_DB", self.old_provider_db)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _restore_env(name: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    @staticmethod
    def _csrf(client) -> str:
        return client.cookies.get("arm_csrf")

    def _login(self, client=None, username="admin", password=None):
        target = client or self.client
        return target.post(
            "/api/auth/login",
            json={
                "username": username,
                "password": password or self.admin_password,
            },
        )

    def _create_user(self, username="alice") -> tuple[dict, str]:
        response = self.client.post(
            "/api/auth/users",
            headers={"X-CSRF-Token": self._csrf(self.client)},
            json={"username": username, "is_admin": False},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["user"], response.json()["temporary_password"]

    def test_service_is_closed_by_default_but_health_and_login_are_public(self):
        root = self.client.get("/", follow_redirects=False)
        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login?next=%2F")

        # A missing path can come from a local source link in a recorded
        # session. It must never become the post-login destination.
        missing = self.client.get(
            "/data01/home/project/README.md%3A1",
            follow_redirects=False,
        )
        self.assertEqual(missing.status_code, 303)
        self.assertEqual(missing.headers["location"], "/login?next=%2F")

        api = self.client.get("/api/sessions")
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.json()["code"], "authentication_required")
        self.assertEqual(api.headers["cache-control"], "no-store")

        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/login").status_code, 200)
        for asset in (
            "/math-renderer.js",
            "/katex/katex-swap.min.css",
            "/katex/fonts/KaTeX_Main-Regular.woff2",
        ):
            response = self.client.get(asset)
            self.assertEqual(response.status_code, 200, asset)
            self.assertNotEqual(response.headers.get("cache-control"), "no-store")
        status = self.client.get("/api/auth/status").json()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["initialized"])

    def test_session_cookie_and_csrf_protect_mutations(self):
        response = self._login()
        self.assertEqual(response.status_code, 200, response.text)
        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(
            value for value in cookies if value.startswith("arm_session=")
        )
        csrf_cookie = next(
            value for value in cookies if value.startswith("arm_csrf=")
        )
        self.assertIn("HttpOnly", session_cookie)
        self.assertIn("SameSite=strict", session_cookie)
        self.assertNotIn("HttpOnly", csrf_cookie)
        self.assertNotIn(self.admin_password, "\n".join(cookies))

        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        rejected = self.client.post("/api/auth/logout")
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json()["code"], "csrf_failed")

        accepted = self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": self._csrf(self.client)},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_authenticated_missing_browser_page_returns_to_home(self):
        self.assertEqual(self._login().status_code, 200)

        missing_page = self.client.get(
            "/data01/home/project/README.md%3A1",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        self.assertEqual(missing_page.status_code, 303)
        self.assertEqual(missing_page.headers["location"], "/")

        # API clients still receive a real 404 instead of HTML navigation.
        missing_api = self.client.get("/api/not-a-real-endpoint")
        self.assertEqual(missing_api.status_code, 404)

    def test_repeated_password_failures_are_committed_and_lock_the_account(self):
        for attempt in range(5):
            response = self._login(password="definitely-wrong-password")
            expected = 429 if attempt == 4 else 401
            self.assertEqual(response.status_code, expected, response.text)
        correct_during_lockout = self._login()
        self.assertEqual(correct_during_lockout.status_code, 429)
        self.assertGreater(int(correct_during_lockout.headers["retry-after"]), 0)

    def test_each_user_reads_and_writes_a_different_physical_database(self):
        import app.auth as auth
        import app.database as database
        from app.tenant import tenant_scope

        self.assertEqual(self._login().status_code, 200)
        created, temporary_password = self._create_user()
        alice = auth.auth_repository.get_user(created["id"])

        with tenant_scope(self.admin["data_scope"]):
            admin_path = database.get_db_path()
            database.upsert_session({
                "id": "same-session-id",
                "source": "codex",
                "server_name": "admin-device",
                "title": "admin private conversation",
            })
            database.insert_messages("same-session-id", [{
                "role": "user", "content": "admin-stream-secret"
            }])
        with tenant_scope(alice["data_scope"]):
            alice_path = database.get_db_path()
            database.upsert_session({
                "id": "same-session-id",
                "source": "claude",
                "server_name": "alice-device",
                "title": "alice private conversation",
            })
            database.insert_messages("same-session-id", [{
                "role": "user", "content": "alice-stream-secret"
            }])
        self.assertNotEqual(admin_path, alice_path)
        self.assertTrue(alice_path.startswith(os.path.join(self.tmpdir, "users")))

        from fastapi.testclient import TestClient
        from app.main import app

        alice_client = TestClient(app, raise_server_exceptions=False)
        login = self._login(alice_client, "alice", temporary_password)
        self.assertTrue(login.json()["user"]["must_change_password"])
        blocked = alice_client.get("/api/sessions")
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["code"], "password_change_required")

        changed = alice_client.post(
            "/api/auth/change-password",
            headers={"X-CSRF-Token": self._csrf(alice_client)},
            json={
                "current_password": temporary_password,
                "new_password": "Alice-new-password-2026",
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)

        alice_providers = alice_client.get("/api/model-providers").json()["items"]
        admin_providers = self.client.get("/api/model-providers").json()["items"]
        self.assertTrue(alice_providers[0]["use_proxy"])
        self.assertTrue(admin_providers[0]["use_proxy"])
        alice_proxy = alice_client.patch(
            f"/api/model-providers/{alice_providers[0]['id']}",
            headers={"X-CSRF-Token": self._csrf(alice_client)},
            json={"use_proxy": False},
        )
        self.assertEqual(alice_proxy.status_code, 200, alice_proxy.text)
        self.assertFalse(alice_proxy.json()["use_proxy"])
        self.assertTrue(
            self.client.get("/api/model-providers").json()["items"][0]["use_proxy"]
        )

        alice_session = alice_client.get("/api/sessions/same-session-id")
        admin_session = self.client.get("/api/sessions/same-session-id")
        self.assertEqual(alice_session.json()["title"], "alice private conversation")
        self.assertEqual(admin_session.json()["title"], "admin private conversation")
        self.assertNotIn("admin private", alice_session.text)
        self.assertNotIn("alice private", admin_session.text)

        # Export bodies are streamed after the route returns; their generators
        # must retain the same tenant context as the request.
        alice_export = alice_client.get(
            "/api/export/session/same-session-id?format=json"
        )
        admin_export = self.client.get(
            "/api/export/session/same-session-id?format=json"
        )
        self.assertIn("alice-stream-secret", alice_export.text)
        self.assertNotIn("admin-stream-secret", alice_export.text)
        self.assertIn("admin-stream-secret", admin_export.text)
        self.assertNotIn("alice-stream-secret", admin_export.text)
        self.assertEqual(
            alice_client.get("/api/diagnostics/runtime").status_code, 403
        )
        self.assertEqual(alice_client.get("/metrics").status_code, 403)
        self.assertEqual(alice_client.get("/health/ready").status_code, 200)

        # A newly created user is not seeded with the administrator's YAML
        # devices and cannot create a service-host local-directory profile.
        self.assertEqual(alice_client.get("/api/sync/devices").json()["items"], [])
        local_device = alice_client.post(
            "/api/sync/devices",
            headers={"X-CSRF-Token": self._csrf(alice_client)},
            json={"name": "host-local", "host": "127.0.0.1", "is_local": True},
        )
        self.assertEqual(local_device.status_code, 400)
        alice_client.close()

    def test_bearer_token_has_tenant_scope_and_plaintext_is_never_stored(self):
        import app.auth as auth
        import app.database as database
        from app.tenant import tenant_scope
        from fastapi.testclient import TestClient
        from app.main import app

        self.assertEqual(self._login().status_code, 200)
        with tenant_scope(self.admin["data_scope"]):
            database.upsert_session({
                "id": "bearer-visible",
                "source": "codex",
                "server_name": "admin-device",
                "title": "admin bearer content",
            })
        issued = self.client.post(
            "/api/auth/tokens",
            headers={"X-CSRF-Token": self._csrf(self.client)},
            json={"name": "test-cli", "expires_days": 30},
        )
        self.assertEqual(issued.status_code, 201, issued.text)
        token = issued.json()["token"]

        bearer = TestClient(app, raise_server_exceptions=False)
        visible = bearer.get(
            "/api/sessions/bearer-visible",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(visible.status_code, 200, visible.text)
        # Bearer requests do not rely on browser cookies and therefore do not
        # require a CSRF token.
        second_token = bearer.post(
            "/api/auth/tokens",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "automation"},
        )
        self.assertEqual(second_token.status_code, 201, second_token.text)

        with open(auth.get_auth_db_path(), "rb") as handle:
            auth_bytes = handle.read()
        self.assertNotIn(token.encode(), auth_bytes)
        self.assertNotIn(self.admin_password.encode(), auth_bytes)
        bearer.close()

    def test_disabling_an_account_revokes_access_but_preserves_its_data(self):
        import app.auth as auth
        import app.database as database
        from app.tenant import tenant_scope

        self.assertEqual(self._login().status_code, 200)
        created, password = self._create_user("retained-user")
        user = auth.auth_repository.get_user(created["id"])
        with tenant_scope(user["data_scope"]):
            path = database.get_db_path()
            database.upsert_session({
                "id": "retained-after-disable",
                "source": "codex",
                "server_name": "remote",
                "title": "must remain on disk",
            })

        disabled = self.client.patch(
            f"/api/auth/users/{created['id']}",
            headers={"X-CSRF-Token": self._csrf(self.client)},
            json={"is_active": False},
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(disabled.json()["user"]["is_active"])
        failed_login = self._login(
            username="retained-user", password=password
        )
        self.assertEqual(failed_login.status_code, 401)
        self.assertTrue(os.path.exists(path))
        with sqlite3.connect(path) as conn:
            title = conn.execute(
                "SELECT title FROM sessions WHERE id='retained-after-disable'"
            ).fetchone()[0]
        self.assertEqual(title, "must remain on disk")

    def test_background_sync_enrichment_and_search_keep_the_owner_scope(self):
        import threading
        from unittest.mock import patch

        import app.auth as auth
        import app.database as database
        import app.scheduler as scheduler
        from app.enrichment import EnrichmentManager
        from app.scheduler import SyncManager
        from app.smart_search import SmartSearchManager
        from app.tenant import tenant_scope

        user = auth.auth_repository.create_user(
            "worker-user",
            "Worker-user-password-2026",
        )
        with tenant_scope(user["data_scope"]):
            expected_path = database.get_db_path()

        observed: dict[str, str] = {}

        def fake_sync(force=False):
            observed["sync"] = database.get_db_path()
            return {"codex": {"synced": 0, "skipped": 0, "errors": 0}}

        manager = SyncManager(user["data_scope"])
        with patch.object(scheduler, "sync_local", fake_sync):
            self.assertEqual(manager.submit("local")["status"], "started")
            self.assertTrue(manager.wait(timeout=5))

        enrichment = EnrichmentManager(user["data_scope"])
        enrichment._supervised_loop = lambda: observed.__setitem__(
            "enrichment", database.get_db_path()
        )
        enrichment._supervised_loop_for_scope()

        search_gate = threading.Event()
        search = SmartSearchManager()

        def fake_search(_query, _filters):
            observed["search"] = database.get_db_path()
            search_gate.wait(timeout=2)
            return {"plan": None, "results": []}

        search._execute = fake_search
        with tenant_scope(user["data_scope"]):
            submitted = search.submit("private query")
        thread = search._threads[submitted["job_id"]]
        search_gate.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

        self.assertEqual(observed, {
            "sync": expected_path,
            "enrichment": expected_path,
            "search": expected_path,
        })


if __name__ == "__main__":
    unittest.main()
