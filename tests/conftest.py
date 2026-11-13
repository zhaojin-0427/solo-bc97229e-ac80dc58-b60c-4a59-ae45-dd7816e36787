"""pytest 公共夹具。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.demo import build_demo_db  # noqa: E402
from app.schema import introspect  # noqa: E402


@pytest.fixture()
def source_db(tmp_path: Path) -> Path:
    return build_demo_db(tmp_path / "source.db")


@pytest.fixture()
def orphan_db(tmp_path: Path) -> Path:
    return build_demo_db(tmp_path / "orphan.db", with_orphan=True)


@pytest.fixture()
def schema(source_db: Path):
    return introspect(str(source_db))


@pytest.fixture()
def out_path(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    return out / "slice.db"
