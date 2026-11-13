"""Web API 端到端测试（Flask 测试客户端）。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app import create_app
from app.demo import build_demo_db


@pytest.fixture()
def app(tmp_path):
    source = build_demo_db(tmp_path / "source.db")
    app = create_app(
        {
            "SOURCE_DB": str(source),
            "DATA_DIR": str(tmp_path / "data"),
            "BATCH_SIZE": 50,
            "MAX_WORKERS": 2,
            "TESTING": True,
        }
    )
    yield app
    app.extensions["job_manager"].shutdown()


@pytest.fixture()
def client(app):
    return app.test_client()


def _wait(client, job_id):
    for _ in range(200):
        r = client.get(f"/api/jobs/{job_id}")
        body = r.get_json()
        if body["status"] in ("success", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("任务超时")


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "切片".encode() in r.data


def test_create_job_and_download_slice(client):
    r = client.post("/api/jobs", json={"return_order_id": 1})
    assert r.status_code == 201
    job_id = r.get_json()["id"]

    body = _wait(client, job_id)
    assert body["status"] == "success"
    assert body["row_counts"]["return_orders"] == 1

    dl = client.get(f"/api/jobs/{job_id}/download")
    assert dl.status_code == 200
    assert dl.mimetype == "application/x-sqlite3"

    # 下载内容是合法 SQLite 且通过外键检查
    out = Path(__file__).parent / "_downloaded.db"
    out.write_bytes(dl.data)
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT count(*) FROM return_orders"
        ).fetchone()[0] == 1
    finally:
        conn.close()
        out.unlink()


def test_invalid_id_returns_400(client):
    r = client.post("/api/jobs", json={"return_order_id": "abc"})
    assert r.status_code == 400


def test_failed_job_no_download(client):
    r = client.post("/api/jobs", json={"return_order_id": 404})
    body = _wait(client, r.get_json()["id"])
    assert body["status"] == "failed"
    dl = client.get(f"/api/jobs/{body['id']}/download")
    assert dl.status_code == 409


def test_source_overview(client):
    r = client.get("/api/source")
    body = r.get_json()
    assert body["available"] is True
    assert body["tables"]["return_orders"]["rows"] == 3


def test_demo_writer_endpoints(client):
    r = client.post("/api/demo/tick")
    assert r.status_code == 200
    assert r.get_json()["ticks"] == 1
    r = client.post("/api/demo/start")
    assert r.get_json()["running"] is True
    r = client.post("/api/demo/stop")
    assert r.get_json()["running"] is False
