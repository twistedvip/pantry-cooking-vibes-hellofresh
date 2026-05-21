"""Shared pytest fixtures: fresh + seeded migrated DB per test.

Re-implemented standalone for this repo so the test suite runs without the
core's ``tests/conftest.py`` on the path. Requires ``pantry-cooking-vibes``
to be importable (install with ``pip install -e ../..`` from this folder).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pantry_cooking_vibes.db import connect, init_db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh, fully-migrated, canonical-seeded DB for one test."""
    path = tmp_path / "test.db"
    init_db(path)
    return path


@pytest.fixture
def seeded_db_path(db_path: Path) -> Path:
    """``db_path`` plus a couple of recipes + a pantry item for cross-checks."""
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO recipes (source, source_id, name) VALUES "
            "('manual', 'demo-1', 'Demo Recipe One')"
        )
        conn.execute(
            "INSERT INTO recipes (source, source_id, name) VALUES "
            "('manual', 'demo-2', 'Demo Recipe Two')"
        )
    return db_path
