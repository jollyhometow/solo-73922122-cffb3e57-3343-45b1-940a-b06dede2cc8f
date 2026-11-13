"""FastAPI 端到端测试。"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

# 每个测试进程使用独立 DATA_DIR
os.environ.setdefault("DATA_DIR", "/tmp/reducer-test-data")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import DATA_DIR  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as c:
        yield c


def _wait_done(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200
        job = r.json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.2)
    raise AssertionError("任务超时未结束")


def test_index_and_samples(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "SQLite" in r.text
    r = client.get("/api/samples")
    assert r.status_code == 200
    samples = r.json()
    assert set(samples) == {"cyclic", "commit_then_fail"}
    for info in samples.values():
        assert client.get(info["db_url"]).status_code == 200
        assert client.get(info["sql_url"]).status_code == 200


def test_full_job_cyclic_sample(client):
    db = client.get("/static/sample_cyclic.db").content
    sql = client.get("/static/sample_cyclic.sql").content
    r = client.post(
        "/api/jobs",
        files={"db": ("cyclic.db", db, "application/x-sqlite3"),
               "sql": ("cyclic.sql", sql, "text/plain")},
    )
    assert r.status_code == 202, r.text
    job = _wait_done(client, r.json()["id"])
    assert job["status"] == "done"
    assert job["kind"] == "reduced"
    assert job["baseline"]["error_name"] == "SQLITE_CONSTRAINT_PRIMARYKEY"
    assert job["final"]["error_code"] == job["baseline"]["error_code"]
    assert job["final"]["error_message"] == job["baseline"]["error_message"]
    total_before = sum(job["row_counts_before"].values())
    total_after = sum(job["row_counts_after"].values())
    assert total_after < total_before

    dl = client.get(f"/api/jobs/{job['id']}/download")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/x-sqlite3"
    reduced_path = "/tmp/reducer-test-download.db"
    with open(reduced_path, "wb") as fh:
        fh.write(dl.content)
    conn = sqlite3.connect(reduced_path)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # 下载库直接复现同一错误
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.executescript(sql.decode())
        raise AssertionError("下载库应当复现错误")
    except sqlite3.Error as exc:
        assert exc.sqlite_errorcode == job["baseline"]["error_code"]
        assert str(exc) == job["baseline"]["error_message"]
    conn.close()


def test_commit_then_fail_sample_job(client):
    db = client.get("/static/sample_commit_then_fail.db").content
    sql = client.get("/static/sample_commit_then_fail.sql").content
    r = client.post(
        "/api/jobs",
        files={"db": ("nodes.db", db, "application/x-sqlite3")},
        data={"script_text": sql.decode()},
    )
    assert r.status_code == 202
    job = _wait_done(client, r.json()["id"])
    assert job["status"] == "done" and job["kind"] == "reduced"
    assert job["baseline"]["error_name"] == "SQLITE_CONSTRAINT_NOTNULL"
    assert job["download_available"] is True


def test_success_script_no_reduction(client):
    db = client.get("/static/sample_cyclic.db").content
    r = client.post(
        "/api/jobs",
        files={"db": ("cyclic.db", db, "application/x-sqlite3")},
        data={"script_text": "SELECT 1;"},
    )
    job = _wait_done(client, r.json()["id"])
    assert job["status"] == "done"
    assert job["kind"] == "success_no_reduction"
    assert job["download_available"] is False
    dl = client.get(f"/api/jobs/{job['id']}/download")
    assert dl.status_code == 400


def test_reject_bad_upload(client):
    r = client.post(
        "/api/jobs",
        files={"db": ("bad.db", b"hello world not sqlite", "application/octet-stream")},
        data={"script_text": "SELECT 1;"},
    )
    assert r.status_code == 400
    assert "SQLite" in r.json()["detail"]


def test_reject_bad_schema(client, tmp_path):
    path = tmp_path / "bad.db"
    conn = sqlite3.connect(str(path))
    # 4 张表，超出限制
    for i in range(4):
        conn.execute(f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    r = client.post(
        "/api/jobs",
        files={"db": ("bad.db", path.read_bytes(), "application/x-sqlite3")},
        data={"script_text": "SELECT 1;"},
    )
    job = _wait_done(client, r.json()["id"])
    assert job["status"] == "error"
    assert "3 张" in job["message"]


def test_reject_attach_script(client):
    db = client.get("/static/sample_cyclic.db").content
    r = client.post(
        "/api/jobs",
        files={"db": ("cyclic.db", db, "application/x-sqlite3")},
        data={"script_text": "ATTACH '/tmp/x.db' AS x;"},
    )
    job = _wait_done(client, r.json()["id"])
    assert job["status"] == "error"
    assert "ATTACH" in job["message"]


def test_missing_job_400(client):
    r = client.get("/api/jobs/does-not-exist")
    assert r.status_code == 400


def test_missing_script_rejected(client):
    db = client.get("/static/sample_cyclic.db").content
    r = client.post(
        "/api/jobs",
        files={"db": ("cyclic.db", db, "application/x-sqlite3")},
        data={"script_text": ""},
    )
    assert r.status_code == 400
