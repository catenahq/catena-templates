"""Shared fixtures: the catalog sources of this checkout are loaded once per session."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import model  # noqa: E402

_REAL_LOAD = model.load_sources
_REAL_TREE = (model.BLUEPRINTS, model.CARD_SCHEMA, model.BLOCK_SCHEMA)
_loaded: list[model.Entry] = []


@pytest.fixture(autouse=True)
def _load_sources_once(monkeypatch):
    """model.load_sources reads and validates every blueprint. A test that
    reads this checkout's tree gets a copy of one load per session; a test that
    points the loader at another tree reads that tree on each call."""
    def load_sources() -> list[model.Entry]:
        if (model.BLUEPRINTS, model.CARD_SCHEMA, model.BLOCK_SCHEMA) != _REAL_TREE:
            return _REAL_LOAD()
        if not _loaded:
            _loaded.extend(_REAL_LOAD())
        return copy.deepcopy(_loaded)

    monkeypatch.setattr(model, "load_sources", load_sources)
