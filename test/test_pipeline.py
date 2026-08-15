"""流水线控制 API 测试"""
import pytest


@pytest.fixture(autouse=True)
def reset_state():
    """每个用例前重置模块级 _state，避免相互污染"""
    from web.routers import pipeline as pipeline_router

    pipeline_router._state.update({
        "running": False, "pid": None, "started_at": None,
        "finished_at": None, "exit_code": None, "last_output": "",
    })
    yield


class _FakeStream:
    def __init__(self, lines):
        self._lines = list(lines)
        self._idx = 0

    async def readline(self):
        if self._idx < len(self._lines):
            line = self._lines[self._idx]
            self._idx += 1
            return (line + "\n").encode("utf-8")
        return b""


class _FakeProc:
    """模拟 asyncio 子进程：stdout.readline() 返回给定行，wait() 返回 returncode"""
    pid = 4242
    returncode = 0

    def __init__(self, lines=("line1", "line2")):
        self.stdout = _FakeStream(lines)

    async def wait(self):
        return self.returncode


def test_status_initial(client):
    r = client.get("/api/pipeline/status")
    assert r.status_code == 200
    data = r.json()
    assert data["running"] is False
    assert data["output_lines"] == 0


def test_logs_empty(client):
    r = client.get("/api/pipeline/logs")
    assert r.status_code == 200
    assert r.json() == {"lines": [], "total": 0, "running": False}


def test_run_conflict_when_running(client):
    from web.routers import pipeline as pipeline_router

    pipeline_router._state["running"] = True
    r = client.post("/api/pipeline/run", json={"input": "input/shots.csv"})
    assert r.status_code == 409


def test_run_and_finish(client, monkeypatch, workdir):
    import asyncio as _asyncio
    from web.routers import pipeline as pipeline_router

    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr("web.routers.pipeline.asyncio.create_subprocess_exec", fake_exec)

    r = client.post("/api/pipeline/run", json={"input": "input/shots.csv", "name": "batch_x"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    for _ in range(50):  # 等待异步任务结束（最多 2 秒）
        if not pipeline_router._state["running"]:
            break
        import time

        time.sleep(0.04)

    assert captured["cmd"][:2] == ["python", "cli.py"]
    assert "--input" in captured["cmd"]
    assert "--name" in captured["cmd"]

    assert pipeline_router._state["running"] is False
    assert pipeline_router._state["exit_code"] == 0
    assert pipeline_router._state["finished_at"] is not None

    status = client.get("/api/pipeline/status").json()
    assert status["running"] is False
    assert status["exit_code"] == 0
    assert status["output_lines"] == 2

    logs = client.get("/api/pipeline/logs").json()
    assert logs["lines"] == ["line1", "line2"]


# ── 批量生成（跨表格选择镜头）────────────────────────────

def _seed_table(client, shot_ids=("1", "2")):
    client.post("/api/tables", json={"name": "T"})
    for sid in shot_ids:
        client.post("/api/tables/1/shots", json={"id": sid, "duration": 4, "scene_desc": f"场景{sid}",
                                                 "dialogue": f"台词{sid}", "screen_text": "",
                                                 "asset_type": "ai_generated", "asset_path": ""})


def test_generate_cross_table(client, monkeypatch, workdir):
    from web.routers import pipeline as pipeline_router

    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr("web.routers.pipeline.asyncio.create_subprocess_exec", fake_exec)

    _seed_table(client)
    client.post("/api/tables", json={"name": "T2"})
    client.post("/api/tables/2/shots", json={"id": "9", "duration": 3, "scene_desc": "另一表格",
                                             "dialogue": "跨表", "screen_text": "",
                                             "asset_type": "ai_generated", "asset_path": ""})

    r = client.post("/api/generate", json={
        "name": "batch_x",
        "selections": [{"table_id": "1", "shot_ids": ["1", "2"]}, {"table_id": "2", "shot_ids": ["9"]}],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["batch"] == "batch_x"

    for _ in range(50):
        if not pipeline_router._state["running"]:
            break
        import time
        time.sleep(0.04)
    # 等异步任务真正完成（asyncio.create_task 不阻塞）
    import time as _time
    for _ in range(20):
        _time.sleep(0.05)
        if not pipeline_router._state["running"]:
            break

    cmd = captured["cmd"]
    assert cmd[:2] == ["python", "cli.py"]
    assert "--skip-merge" in cmd
    assert "--name" in cmd and cmd[cmd.index("--name") + 1] == "batch_x"
    csv_path = cmd[cmd.index("--input") + 1]
    assert "tmp_gen" in csv_path

    # 临时 CSV 已被清理
    import os
    assert not os.path.exists(csv_path)


def test_generate_marks_done(client, monkeypatch, workdir):
    import time
    from web.routers import pipeline as pipeline_router

    async def fake_exec(*cmd, **kwargs):
        return _FakeProc()

    monkeypatch.setattr("web.routers.pipeline.asyncio.create_subprocess_exec", fake_exec)

    _seed_table(client, shot_ids=("1",))

    # 预置输出视频（模拟生成成功）
    shots_dir = workdir / "output" / "batch_x" / "shots"
    shots_dir.mkdir(parents=True)
    (shots_dir / "batch_x_shot_1.mp4").write_bytes(b"x")

    client.post("/api/generate", json={"name": "batch_x", "selections": [{"table_id": "1", "shot_ids": ["1"]}]})

    for _ in range(50):
        if not pipeline_router._state["running"]:
            break
        time.sleep(0.04)
    time.sleep(0.2)  # 等 on_done 回调完成状态回写

    shots = client.get("/api/tables/1/shots").json()
    assert shots[0]["status"] == "done"


def test_generate_conflict_when_running(client):
    from web.routers import pipeline as pipeline_router

    pipeline_router._state["running"] = True
    r = client.post("/api/generate", json={"selections": [{"table_id": "1", "shot_ids": ["1"]}]})
    assert r.status_code == 409


def test_generate_no_selection(client):
    r = client.post("/api/generate", json={"name": "x", "selections": []})
    assert r.status_code == 400