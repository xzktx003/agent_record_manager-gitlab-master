"""Request and worker-local data-scope propagation."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


LEGACY_DATA_SCOPE = "legacy"
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_current_data_scope: ContextVar[str] = ContextVar(
    "arm_data_scope",
    default=LEGACY_DATA_SCOPE,
)


def validate_data_scope(scope: str) -> str:
    value = str(scope or "").strip()
    if not _SCOPE_RE.fullmatch(value):
        raise ValueError("invalid data scope")
    return value


def get_data_scope() -> str:
    return _current_data_scope.get()


@contextmanager
def tenant_scope(scope: str) -> Iterator[None]:
    token = _current_data_scope.set(validate_data_scope(scope))
    try:
        yield
    finally:
        _current_data_scope.reset(token)
