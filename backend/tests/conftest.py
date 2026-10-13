"""Isolate the configured durable Sentinel approval database per test."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest


# A few legacy execution test modules construct approval helpers while their
# module is imported, before fixtures run. Give those compatibility helpers one
# unique, process-local test database so they never touch the runtime default.
_COLLECTION_DB = Path(tempfile.mkdtemp(prefix="friday-shared-authority-")) / "authority.sqlite3"
os.environ["FRIDAY_SHARED_AUTHORITY_DB_PATH"] = str(_COLLECTION_DB)


@pytest.fixture(autouse=True)
def _isolated_shared_authority_database(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("FRIDAY_SHARED_AUTHORITY_DB_PATH", str(tmp_path / "shared-authority.sqlite3"))
