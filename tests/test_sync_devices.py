"""Persistent sync-device management regression tests."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock


class TestSyncDevices(unittest.TestCase):
    def setUp(self):
        import app.config as config
        import app.database as database
        import app.sync.device_store as device_store

        self.tmpdir = tempfile.mkdtemp()
        self._old_device_db = os.environ.get("ARM_SYNC_DEVICE_DB")
        self._old_db_path = database._db_path
        os.environ["ARM_SYNC_DEVICE_DB"] = os.path.join(
            self.tmpdir, "devices.db"
        )
        database._db_path = os.path.join(self.tmpdir, "sessions.db")
        database.init_db()
        config._config = config.AppConfig(
            servers=[
                config.ServerConfig(
                    name="Workstation",
                    host="127.0.0.1",
                    codex_dir=os.path.join(self.tmpdir, "codex"),
                    opencode_dir="",
                    is_local=True,
                ),
                config.ServerConfig(
                    name="Remote-1",
                    host="192.0.2.10",
                    user="tester",
                    password="initial-secret",
                ),
            ]
        )
        device_store._schema_path = None
        device_store.initialize_sync_devices()

    def tearDown(self):
        import app.config as config
        import app.database as database
        import app.sync.device_store as device_store

        device_store._schema_path = None
        config._config = None
        database._db_path = self._old_db_path
        if self._old_device_db is None:
            os.environ.pop("ARM_SYNC_DEVICE_DB", None)
        else:
            os.environ["ARM_SYNC_DEVICE_DB"] = self._old_device_db
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_yaml_devices_import_once_and_secrets_are_write_only(self):
        import app.config as config
        import app.sync.device_store as device_store

        items = device_store.sync_device_repository.list()
        self.assertEqual({item["name"] for item in items}, {
            "Workstation", "Remote-1",
        })
        remote = next(item for item in items if item["name"] == "Remote-1")
        self.assertNotIn("password", remote)
        self.assertTrue(remote["credential_configured"])
        self.assertEqual(
            device_store.sync_device_repository.get_config(remote["id"]).password,
            "initial-secret",
        )

        # A process restart must not re-import later YAML changes over runtime
        # edits or resurrect a device the user deleted.
        device_store.sync_device_repository.delete(remote["id"])
        config._config.servers.append(config.ServerConfig(
            name="Late-YAML", host="192.0.2.11"
        ))
        device_store._schema_path = None
        device_store.initialize_sync_devices()
        self.assertEqual(
            [item["name"] for item in device_store.sync_device_repository.list()],
            ["Workstation"],
        )

    def test_create_update_and_delete_preserve_synced_sessions(self):
        from fastapi.testclient import TestClient

        from app.database import get_session_detail, upsert_session
        from app.main import app

        client = TestClient(app, raise_server_exceptions=False)
        created_response = client.post("/api/sync/devices", json={
            "name": "Remote-New",
            "host": "192.0.2.20",
            "user": "alice",
            "password": "write-only-secret",
        })
        self.assertEqual(created_response.status_code, 201, created_response.text)
        created = created_response.json()
        self.assertNotIn("password", created)
        self.assertTrue(created["credential_configured"])

        updated = client.patch(
            f"/api/sync/devices/{created['id']}",
            json={"host": "192.0.2.21", "password": ""},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertTrue(updated.json()["credential_configured"])

        upsert_session({
            "id": "remote-new-history",
            "source": "codex",
            "server_name": "Remote-New",
            "title": "Retained history",
        })
        listed = client.get("/api/sync/devices").json()["items"]
        listed_device = next(item for item in listed if item["id"] == created["id"])
        self.assertEqual(listed_device["retained_session_count"], 1)

        deleted = client.delete(f"/api/sync/devices/{created['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["retained_sessions"], 1)
        self.assertIsNotNone(get_session_detail("remote-new-history"))
        remaining_ids = {
            item["id"] for item in client.get("/api/sync/devices").json()["items"]
        }
        self.assertNotIn(created["id"], remaining_ids)

    def test_remote_transition_requires_host(self):
        import app.sync.device_store as device_store

        local = next(
            item for item in device_store.sync_device_repository.list()
            if item["is_local"]
        )
        device_store.sync_device_repository.update(local["id"], {"host": ""})
        with self.assertRaisesRegex(device_store.SyncDeviceError, "主机地址"):
            device_store.sync_device_repository.update(
                local["id"], {"is_local": False}
            )

    def test_deleted_local_profile_stops_collection(self):
        import app.sync.device_store as device_store
        from app.sync.ssh_sync import sync_local

        local = next(
            item for item in device_store.sync_device_repository.list()
            if item["is_local"]
        )
        device_store.sync_device_repository.delete(local["id"])
        result = sync_local()
        self.assertTrue(result["skipped"])
        self.assertIn("本机同步设备", result["reason"])

    def test_remote_commands_always_receive_a_read_timeout(self):
        from app.config import ServerConfig
        from app.sync.ssh_sync import SSHSyncer

        syncer = SSHSyncer(ServerConfig(name="bounded", host="192.0.2.30"))
        syncer.client = MagicMock()
        syncer._exec_command("find /tmp -type f")
        syncer.client.exec_command.assert_called_once_with(
            "find /tmp -type f", timeout=120
        )


if __name__ == "__main__":
    unittest.main()
