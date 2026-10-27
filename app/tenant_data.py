"""Initialize one user's physically isolated data stores."""

from __future__ import annotations

import threading

from app.database import get_db_path, init_db
from app.model_provider import initialize_model_providers
from app.sync.device_store import initialize_sync_devices
from app.tenant import tenant_scope


_initialized_paths: set[str] = set()
_initialize_lock = threading.Lock()


def ensure_tenant_data(scope: str) -> None:
    with tenant_scope(scope):
        path = get_db_path()
        if path in _initialized_paths:
            return
        with _initialize_lock:
            if path in _initialized_paths:
                return
            init_db()
            initialize_model_providers()
            initialize_sync_devices()
            _initialized_paths.add(path)
