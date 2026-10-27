"""Tests for persistent model-provider management."""

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class ModelProviderFixture(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import app.config as config
        import app.database as db
        import app.model_provider as providers

        self.old_config = config._config
        config._config = config.AppConfig(
            auth=config.AuthConfig(enabled=False),
            sync=config.SyncConfig(auto_start=False),
            agent=config.AgentConfig(enabled=False, auto_start_worker=False),
        )
        self.config = config
        db._db_path = os.path.join(self.tmpdir, "model-providers.db")
        db.init_db()
        providers._service = None
        self.db = db
        self.providers = providers
        self.service = providers.get_model_provider_service()

    def tearDown(self):
        self.providers._service = None
        self.config._config = self.old_config
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestModelProviderService(ModelProviderFixture):
    def test_existing_opencode_profile_is_preserved(self):
        from app.model_provider import (
            BUILTIN_CODEX_ID,
            CreateModelProviderRequest,
            ModelProviderService,
        )

        opencode = self.service.create(CreateModelProviderRequest(
            name="OpenCode CLI",
            kind="opencode_cli",
            model="newapi/gpt-5.6-sol",
            executable="opencode",
            reasoning_effort="medium",
        ))
        ModelProviderService().initialize()
        self.assertEqual(self.service.get(opencode.id).name, "OpenCode CLI")
        self.assertEqual(self.service.get().id, BUILTIN_CODEX_ID)

    def test_provider_save_is_independent_from_main_database_write_lock(self):
        from app.model_provider import BUILTIN_CODEX_ID, UpdateModelProviderRequest

        with self.db.get_db() as main_connection:
            main_connection.execute("BEGIN IMMEDIATE")
            selected = self.service.update(
                BUILTIN_CODEX_ID,
                UpdateModelProviderRequest(
                    model="gpt-5.6-terra",
                    reasoning_effort="high",
                ),
            )
        self.assertEqual(selected.model, "gpt-5.6-terra")
        self.assertEqual(selected.reasoning_effort, "high")

    def test_legacy_profiles_and_secrets_migrate_without_deleting_source(self):
        from app.model_provider import ModelProviderService

        with self.db.get_db() as source:
            source.executescript("""
                CREATE TABLE model_providers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '', base_url TEXT, credential_ref TEXT,
                    executable TEXT NOT NULL DEFAULT '', reasoning_effort TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL,
                    is_default INTEGER NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE model_provider_secrets (
                    provider_id TEXT PRIMARY KEY, secret_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            source.execute(
                """INSERT INTO model_providers VALUES (
                       'legacy-ark','火山方舟','openai_compatible','ark-code-latest',
                       'https://ark.example/v3',NULL,'','max',300,1,0,
                       '2026-01-01','2026-01-02')"""
            )
            source.execute(
                "INSERT INTO model_provider_secrets VALUES "
                "('legacy-ark','saved-secret','2026-01-02')"
            )
        with self.providers._provider_db() as target:
            target.execute(
                "DELETE FROM model_provider_meta WHERE key='legacy_main_db_migrated'"
            )

        migrated = ModelProviderService()
        migrated.initialize()

        self.assertEqual(migrated.get("legacy-ark").name, "火山方舟")
        self.assertFalse(migrated.get("legacy-ark").use_proxy)
        self.assertEqual(migrated.repository.get_secret("legacy-ark"), "saved-secret")
        with self.db.get_db() as source:
            self.assertIsNotNone(source.execute(
                "SELECT 1 FROM model_providers WHERE id='legacy-ark'"
            ).fetchone())

    def test_existing_provider_database_gains_proxy_column_safely(self):
        from app.model_provider import BUILTIN_CODEX_ID, ModelProviderRepository

        provider_path = self.providers.get_model_provider_db_path()
        for candidate in (provider_path, f"{provider_path}-wal", f"{provider_path}-shm"):
            try:
                os.remove(candidate)
            except FileNotFoundError:
                pass
        with sqlite3.connect(provider_path) as connection:
            connection.executescript("""
                CREATE TABLE model_providers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '', base_url TEXT, credential_ref TEXT,
                    executable TEXT NOT NULL DEFAULT '', reasoning_effort TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL,
                    is_default INTEGER NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            connection.execute(
                "INSERT INTO model_providers VALUES "
                "(?, 'Codex CLI', 'codex_cli', '', NULL, NULL, 'codex', "
                "'high', 600, 1, 1, '2026-01-01', '2026-01-01')",
                (BUILTIN_CODEX_ID,),
            )

        repository = ModelProviderRepository()
        repository.ensure_schema()
        migrated = repository.get(BUILTIN_CODEX_ID)
        self.assertEqual(migrated["use_proxy"], 1)

    def test_seeded_cli_provider_and_saved_secret_are_write_only(self):
        from app.model_provider import CreateModelProviderRequest

        initial = self.service.list()
        self.assertEqual(len(initial), 1)
        self.assertTrue(initial[0].is_default)
        self.assertEqual(initial[0].kind, "codex_cli")
        self.assertTrue(initial[0].managed)
        self.assertEqual(initial[0].model, "")
        self.assertEqual(initial[0].reasoning_effort, "inherit")
        self.assertTrue(initial[0].use_proxy)

        created = self.service.create(CreateModelProviderRequest(
            name="火山方舟",
            kind="openai_compatible",
            model="ark-code-latest",
            base_url="https://ark.cn-beijing.volces.com/api/plan/v3/",
            api_key="ark-secret-value-that-must-not-leak",
            reasoning_effort="max",
        ))
        payload = created.model_dump(mode="json")
        self.assertTrue(payload["credential_configured"])
        self.assertEqual(payload["credential_source"], "saved")
        self.assertNotIn("credential_ref", payload)
        self.assertNotIn("secret", str(payload))
        self.assertEqual(created.base_url, "https://ark.cn-beijing.volces.com/api/plan/v3")
        self.assertFalse(created.use_proxy)

    def test_proxy_choice_is_saved_and_controls_cli_environment(self):
        from app.agent_provider import _provider_process_env
        from app.model_provider import BUILTIN_CODEX_ID, UpdateModelProviderRequest

        with patch.dict(os.environ, {
            "HTTP_PROXY": "http://proxy.example:8080",
            "https_proxy": "http://proxy.example:8080",
            "NO_PROXY": "localhost",
        }):
            inherited = _provider_process_env(self.service.get(BUILTIN_CODEX_ID))
            self.assertEqual(inherited["HTTP_PROXY"], "http://proxy.example:8080")
            self.assertEqual(inherited["https_proxy"], "http://proxy.example:8080")

            direct = self.service.update(
                BUILTIN_CODEX_ID,
                UpdateModelProviderRequest(use_proxy=False),
            )
            stripped = _provider_process_env(direct)
            self.assertFalse(any(
                name.casefold().endswith("proxy") for name in stripped
            ))

        self.assertFalse(self.service.get(BUILTIN_CODEX_ID).use_proxy)

    def test_initialization_migration_runs_only_once(self):
        from unittest.mock import patch

        service = self.providers.ModelProviderService()
        with patch.object(
            service.repository,
            "ensure_builtin_codex",
            wraps=service.repository.ensure_builtin_codex,
        ) as initialize:
            service.initialize()
            service.initialize()
            service.list()
            service.get()
        self.assertEqual(initialize.call_count, 1)

    def test_switching_default_is_atomic_and_default_cannot_be_disabled(self):
        from app.model_provider import (
            CreateModelProviderRequest,
            ModelProviderError,
            UpdateModelProviderRequest,
        )

        hm = self.service.create(CreateModelProviderRequest(
            name="HMDeepSeek",
            kind="openai_compatible",
            model="DeepSeek-V4-Flash-0731",
            base_url="http://10.30.0.21:8005/v1",
        ))
        selected = self.service.update(
            hm.id, UpdateModelProviderRequest(is_default=True, enabled=True)
        )
        self.assertTrue(selected.is_default)
        self.assertEqual(sum(item.is_default for item in self.service.list()), 1)
        with self.assertRaises(ModelProviderError):
            self.service.update(hm.id, UpdateModelProviderRequest(enabled=False))

    def test_switching_default_preserves_connection_health(self):
        from app.model_provider import (
            CreateModelProviderRequest,
            UpdateModelProviderRequest,
        )

        hm = self.service.create(CreateModelProviderRequest(
            name="HMDeepSeek",
            kind="openai_compatible",
            model="DeepSeek-V4-Flash-0731",
            base_url="http://10.30.0.21:8005/v1",
        ))
        self.service.repository.set_health(hm.id, {
            "available": True,
            "checked_at": "2026-08-12T00:00:00+00:00",
            "detail": "ready",
            "latency_ms": 10,
        })

        self.service.update(hm.id, UpdateModelProviderRequest(is_default=True))
        self.assertTrue(self.service.repository.get_health(hm.id)["available"])

        self.service.update(
            hm.id,
            UpdateModelProviderRequest(
                model=hm.model,
                reasoning_effort=hm.reasoning_effort,
                timeout_seconds=hm.timeout_seconds,
                is_default=True,
            ),
        )
        self.assertTrue(self.service.repository.get_health(hm.id)["available"])

        self.service.update(
            hm.id,
            UpdateModelProviderRequest(model="DeepSeek-V4-Flash-next"),
        )
        self.assertEqual(self.service.repository.get_health(hm.id), {})

    def test_builtin_codex_can_select_model_and_effort_but_cannot_be_deleted(self):
        from app.model_provider import (
            BUILTIN_CODEX_ID,
            ModelProviderError,
            UpdateModelProviderRequest,
        )

        selected = self.service.update(
            BUILTIN_CODEX_ID,
            UpdateModelProviderRequest(
                model="gpt-5.6-terra",
                reasoning_effort="high",
            ),
        )
        self.assertEqual(selected.model, "gpt-5.6-terra")
        self.assertEqual(selected.reasoning_effort, "high")
        with self.assertRaises(ModelProviderError):
            self.service.update(
                BUILTIN_CODEX_ID,
                UpdateModelProviderRequest(name="Renamed Codex"),
            )
        with self.assertRaises(ModelProviderError):
            self.service.delete(BUILTIN_CODEX_ID)

    def test_codex_catalog_returns_only_visible_models_and_local_defaults(self):
        import json
        from unittest.mock import patch

        catalog = {
            "models": [
                {
                    "slug": "gpt-visible",
                    "display_name": "Visible",
                    "description": "ready",
                    "visibility": "list",
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [
                        {"effort": "low"}, {"effort": "high"}
                    ],
                },
                {"slug": "gpt-hidden", "visibility": "hide"},
            ]
        }
        completed = type("Completed", (), {
            "returncode": 0,
            "stdout": json.dumps(catalog),
            "stderr": "",
        })()
        with patch("app.model_provider.subprocess.run", return_value=completed), \
             patch("app.model_provider._codex_local_defaults", return_value={
                 "model": "gpt-local", "reasoning_effort": "max"
             }):
            result = self.service.codex_catalog()
        self.assertEqual([item["id"] for item in result["models"]], ["gpt-visible"])
        self.assertEqual(result["models"][0]["reasoning_efforts"], ["low", "high"])
        self.assertEqual(result["default_model"], "gpt-local")
        self.assertEqual(result["default_reasoning_effort"], "max")

    def test_connection_test_returns_diagnostic_when_health_write_is_locked(self):
        import sqlite3
        from unittest.mock import patch
        from app.agent_provider import ProviderDiagnostic
        from app.model_provider import BUILTIN_CODEX_ID

        diagnostic = ProviderDiagnostic(
            available=True,
            stage="ready",
            detail="真实结构化推理已通过（gpt-test）。",
            latency_ms=15,
            tested_url="codex",
        )
        with patch("app.agent_provider.CodexCLIProvider.diagnose", return_value=diagnostic), \
             patch.object(
                 self.service.repository,
                 "set_health",
                 side_effect=sqlite3.OperationalError("database is locked"),
             ):
            result = self.service.status(BUILTIN_CODEX_ID)
        self.assertTrue(result["available"])
        self.assertIn("未持久化", result["suggestions"][-1])
        self.assertTrue(result["profile"]["last_test_ok"])

    def test_openai_compatible_provider_supports_native_and_agent_plan_bodies(self):
        from app.agent_provider import OpenAICompatibleProvider
        from app.model_provider import CreateModelProviderRequest

        hm = self.service.create(CreateModelProviderRequest(
            name="HMDeepSeek",
            kind="openai_compatible",
            model="DeepSeek-V4-Flash-0731",
            base_url="http://10.30.0.21:8005/v1",
        ))
        hm_body = OpenAICompatibleProvider(hm, api_key=None)._structured_body(
            "test", {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        )
        self.assertEqual(hm_body["response_format"]["type"], "json_schema")

        ark = self.service.create(CreateModelProviderRequest(
            name="火山 Agent Plan",
            kind="openai_compatible",
            model="ark-code-latest",
            base_url="https://ark.cn-beijing.volces.com/api/plan/v3",
            api_key="ark-secret-value-that-must-not-leak",
            reasoning_effort="max",
        ))
        provider = OpenAICompatibleProvider(ark, api_key="ark-test")
        body = provider._structured_body(
            "test",
            {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            native_json_schema=False,
            agent_plan=True,
        )
        self.assertNotIn("response_format", body)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["reasoning_effort"], "max")

    def test_openai_compatible_transport_honors_proxy_choice(self):
        from app.agent_provider import OpenAICompatibleProvider
        from app.model_provider import CreateModelProviderRequest

        profile = self.service.create(CreateModelProviderRequest(
            name="Proxied API",
            kind="openai_compatible",
            model="gpt-test",
            base_url="https://models.example/v1",
            use_proxy=True,
        ))
        captured = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "{\"ok\":true}"}}]}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def post(*_args, **_kwargs):
                return FakeResponse()

        provider = OpenAICompatibleProvider(profile, api_key=None)
        with patch("app.agent_provider.httpx.Client", FakeClient):
            result = provider.structured(
                "test",
                {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                },
            )
        self.assertEqual(result.data, {"ok": True})
        self.assertTrue(captured["trust_env"])


class TestModelProviderAPI(ModelProviderFixture):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from app.main import app

        self.client = TestClient(app, raise_server_exceptions=False)

    def test_crud_and_connection_test_endpoint(self):
        created = self.client.post("/api/model-providers", json={
            "name": "HMDeepSeek",
            "kind": "openai_compatible",
            "model": "DeepSeek-V4-Flash-0731",
            "base_url": "http://10.30.0.21:8005/v1",
            "reasoning_effort": "high",
            "timeout_seconds": 300,
            "use_proxy": True,
            "enabled": True,
            "is_default": False,
        })
        self.assertEqual(created.status_code, 201, created.text)
        provider = created.json()
        self.assertTrue(provider["use_proxy"])
        listed = self.client.get("/api/model-providers")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.headers["cache-control"], "no-store")

        diagnostic = type("Diagnostic", (), {
            "available": True,
            "stage": "ready",
            "detail": "真实结构化推理已通过。",
            "latency_ms": 12,
            "tested_url": "http://10.30.0.21:8005/v1/chat/completions",
            "suggested_base_url": None,
            "http_status": 200,
            "discovered_models": ("DeepSeek-V4-Flash-0731",),
            "model_available": True,
            "suggestions": (),
        })()
        with patch("app.agent_provider.OpenAICompatibleProvider.diagnose", return_value=diagnostic):
            tested = self.client.post(
                f"/api/model-providers/{provider['id']}/test"
            )
        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertTrue(tested.json()["available"])

    def test_patch_succeeds_while_main_session_database_is_write_locked(self):
        from app.model_provider import BUILTIN_CODEX_ID

        with self.db.get_db() as main_connection:
            main_connection.execute("BEGIN IMMEDIATE")
            response = self.client.patch(
                f"/api/model-providers/{BUILTIN_CODEX_ID}",
                json={"model": "gpt-5.6-terra", "reasoning_effort": "high"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], "gpt-5.6-terra")


if __name__ == "__main__":
    unittest.main()
