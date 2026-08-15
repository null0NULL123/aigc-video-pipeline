"""镜头 Review & 重做 API 测试"""
import time
import pytest
from fastapi.testclient import TestClient

from pipeline.tracker import get_tracker, reset_tracker
from web.routers import pipeline as pipeline_router


@pytest.fixture(autouse=True)
def _clean():
    """每个测试重置 tracker + pipeline 状态"""
    reset_tracker()
    pipeline_router._state["running"] = False
    pipeline_router._state["exit_code"] = None
    yield
    reset_tracker()
    pipeline_router._state["running"] = False


# ── Helper：预置一个 batch + shot ──

def _seed_tracker(batch_id: str, shot_key: str, status: str = "done",
                  table_id: str = "1", shot_id: str = "1"):
    tracker = get_tracker()
    tracker.update_shot(
        batch_id, shot_key,
        status=status, stage="done",
        table_id=table_id, shot_id=shot_id,
        scene_desc="测试场景", duration=4,
        video_path=f"output/{batch_id}/shots/{batch_id}_shot_{shot_key}.mp4",
    )
    return tracker


# ── GET /api/pipeline/batches ──

def test_list_batches_empty(client):
    r = client.get("/api/pipeline/batches")
    assert r.status_code == 200
    assert r.json()["batches"] == []


def test_list_batches_with_data(client):
    _seed_tracker("batch_001", "1")
    r = client.get("/api/pipeline/batches")
    assert r.status_code == 200
    batches = r.json()["batches"]
    assert len(batches) == 1
    assert batches[0]["batch_id"] == "batch_001"
    assert batches[0]["done"] == 1


# ── GET /api/pipeline/batches/{id}/shots ──

def test_list_batch_shots(client):
    _seed_tracker("batch_001", "1")
    _seed_tracker("batch_001", "2", status="failed")
    r = client.get("/api/pipeline/batches/batch_001/shots")
    assert r.status_code == 200
    shots = r.json()["shots"]
    assert len(shots) == 2
    statuses = {s["key"]: s["status"] for s in shots}
    assert statuses["1"] == "done"
    assert statuses["2"] == "failed"


def test_list_batch_shots_filter_status(client):
    _seed_tracker("batch_001", "1", status="done")
    _seed_tracker("batch_001", "2", status="failed")
    _seed_tracker("batch_001", "3", status="done")
    r = client.get("/api/pipeline/batches/batch_001/shots", params={"status": "done"})
    assert r.status_code == 200
    shots = r.json()["shots"]
    assert len(shots) == 2
    assert {s["key"] for s in shots} == {"1", "3"}


def test_list_batch_shots_not_found(client):
    r = client.get("/api/pipeline/batches/nonexistent/shots")
    assert r.status_code == 404


# ── GET /api/pipeline/batches/{id}/shots/{key} ──

def test_get_batch_shot(client):
    _seed_tracker("batch_001", "1", table_id="t1", shot_id="s1")
    r = client.get("/api/pipeline/batches/batch_001/shots/1")
    assert r.status_code == 200
    data = r.json()
    assert data["key"] == "1"
    assert data["status"] == "done"
    assert data["table_id"] == "t1"
    assert data["shot_id"] == "s1"


def test_get_batch_shot_not_found(client):
    r = client.get("/api/pipeline/batches/batch_001/shots/999")
    assert r.status_code == 404


# ── POST .../confirm ──

def test_confirm_shot(client):
    _seed_tracker("batch_001", "1", status="done")
    r = client.post("/api/pipeline/batches/batch_001/shots/1/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"

    # 验证状态确实变了
    shot = client.get("/api/pipeline/batches/batch_001/shots/1").json()
    assert shot["status"] == "confirmed"


def test_confirm_shot_wrong_status(client):
    _seed_tracker("batch_001", "1", status="failed")
    r = client.post("/api/pipeline/batches/batch_001/shots/1/confirm")
    assert r.status_code == 400


def test_confirm_shot_not_found(client):
    r = client.post("/api/pipeline/batches/batch_001/shots/999/confirm")
    assert r.status_code == 404


# ── POST .../unconfirm ──

def test_unconfirm_shot(client):
    _seed_tracker("batch_001", "1", status="done")
    client.post("/api/pipeline/batches/batch_001/shots/1/confirm")
    r = client.post("/api/pipeline/batches/batch_001/shots/1/unconfirm")
    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_unconfirm_shot_wrong_status(client):
    _seed_tracker("batch_001", "1", status="done")
    r = client.post("/api/pipeline/batches/batch_001/shots/1/unconfirm")
    assert r.status_code == 400  # 只有 confirmed 才能 unconfirm


# ── POST .../redo ──

def test_redo_shot_conflict(client, monkeypatch, workdir):
    pipeline_router._state["running"] = True
    r = client.post("/api/pipeline/batches/batch_001/shots/1/redo")
    assert r.status_code == 409


def test_redo_shot_no_manifest(client, monkeypatch, workdir):
    _seed_tracker("batch_001", "1", status="done")
    # manifest 不存在
    r = client.post("/api/pipeline/batches/batch_001/shots/1/redo")
    assert r.status_code == 404


def test_redo_shot_success(client, monkeypatch, workdir):
    """有 manifest 时，redo 应该启动 subprocess"""
    import asyncio as _asyncio
    captured = {}

    class _FakeStream:
        def __init__(self):
            self._idx = 0
        async def readline(self):
            self._idx += 1
            return b""

    class _FakeProc:
        pid = 5555
        returncode = 0
        stdout = _FakeStream()
        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr("web.routers.pipeline.asyncio.create_subprocess_exec", fake_exec)

    _seed_tracker("batch_001", "1", status="done", table_id="1", shot_id="1")

    # 创建 manifest
    import json
    manifest_dir = workdir / "output" / "batch_001"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "generate_manifest.json").write_text(json.dumps({
        "1": {"table_id": "1", "shot_id": "1", "dialogue": "台词", "screen_text": ""}
    }, ensure_ascii=False), encoding="utf-8")

    # 预置表格数据（test_assets conftest 里 client fixture 可能不带表格）
    client.post("/api/tables", json={"name": "T1"})
    client.post("/api/tables/1/shots", json={
        "id": "1", "duration": 4, "scene_desc": "测试场景",
        "dialogue": "台词", "screen_text": "",
        "asset_type": "ai_generated", "asset_path": "",
    })

    r = client.post("/api/pipeline/batches/batch_001/shots/1/redo")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # shot 状态重置为 pending
    shot = client.get("/api/pipeline/batches/batch_001/shots/1").json()
    assert shot["status"] == "pending"

    # 等异步任务
    import time as _t
    for _ in range(20):
        _t.sleep(0.05)
        if not pipeline_router._state["running"]:
            break

    # 验证调了 cli.py --skip-merge
    assert "cli.py" in captured["cmd"][1]
    assert "--skip-merge" in captured["cmd"]


# ── POST .../merge (只合并 confirmed) ──

def test_merge_confirmed_no_confirmed(client):
    _seed_tracker("batch_001", "1", status="done")
    r = client.post("/api/pipeline/batches/batch_001/merge")
    assert r.status_code == 400
    assert "没有已确认" in r.json()["detail"]


def test_merge_confirmed_conflict(client):
    pipeline_router._state["running"] = True
    r = client.post("/api/pipeline/batches/batch_001/merge")
    assert r.status_code == 409


def test_merge_confirmed_success(client, monkeypatch, workdir):
    import asyncio as _asyncio
    import json
    captured = {}

    class _FakeStream:
        def __init__(self):
            self._idx = 0
        async def readline(self):
            self._idx += 1
            return b""

    class _FakeProc:
        pid = 6666
        returncode = 0
        stdout = _FakeStream()
        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr("web.routers.pipeline.asyncio.create_subprocess_exec", fake_exec)

    _seed_tracker("batch_001", "1", status="done", table_id="1", shot_id="1")
    _seed_tracker("batch_001", "2", status="done", table_id="1", shot_id="2")
    get_tracker().mark_confirmed("batch_001", "1")
    get_tracker().mark_confirmed("batch_001", "2")

    # 创建 manifest
    manifest_dir = workdir / "output" / "batch_001"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "generate_manifest.json").write_text(json.dumps({
        "1": {"table_id": "1", "shot_id": "1", "dialogue": "台词1", "screen_text": ""},
        "2": {"table_id": "1", "shot_id": "2", "dialogue": "台词2", "screen_text": ""},
    }, ensure_ascii=False), encoding="utf-8")

    # 创建表格
    client.post("/api/tables", json={"name": "T1"})
    client.post("/api/tables/1/shots", json={
        "id": "1", "duration": 4, "scene_desc": "场景1",
        "dialogue": "台词1", "screen_text": "",
        "asset_type": "ai_generated", "asset_path": "",
    })
    client.post("/api/tables/1/shots", json={
        "id": "2", "duration": 3, "scene_desc": "场景2",
        "dialogue": "台词2", "screen_text": "",
        "asset_type": "ai_generated", "asset_path": "",
    })

    r = client.post("/api/pipeline/batches/batch_001/merge")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["shot_count"] == 2

    import time as _t
    for _ in range(20):
        _t.sleep(0.05)
        if not pipeline_router._state["running"]:
            break

    assert "cli.py" in captured["cmd"][1]
    assert "--skip-gen" in captured["cmd"]