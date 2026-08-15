"""SSE 实时进度端点测试"""
import asyncio
import json
import pytest
from starlette.testclient import TestClient

from pipeline.tracker import get_tracker, reset_tracker
from web.routers import pipeline as pipeline_router


@pytest.fixture(autouse=True)
def _clean():
    reset_tracker()
    yield
    reset_tracker()


def test_sse_receives_shot_update(client):
    """SSE 能收到 shot 状态变更"""
    tracker = get_tracker()
    batch_id = "sse_batch"

    # 用线程客户端（保持 SSE 连接）
    import threading
    events = []
    ready = threading.Event()

    def reader():
        with client.stream("GET", f"/api/pipeline/batches/{batch_id}/events") as r:
            ready.set()
            for line in r.iter_lines():
                line = line.strip()
                if line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    events.append(data)
                    if data.get("type") == "batch_done":
                        break

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    ready.wait(timeout=2)

    # push 几个事件
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.1))
    tracker.update_shot(batch_id, "1", status="analyzing")
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.1))
    tracker.update_shot(batch_id, "1", status="done")
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.1))
    tracker.mark_done(batch_id, exit_code=0)
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.1))

    t.join(timeout=5)
    assert len(events) >= 3
    assert any(e.get("shot_key") == "1" and e.get("fields", {}).get("status") == "analyzing" for e in events)
    assert any(e.get("shot_key") == "1" and e.get("fields", {}).get("status") == "done" for e in events)
    assert any(e.get("type") == "batch_done" for e in events)


def test_sse_heartbeat(client):
    """SSE 每5秒发心跳"""
    import threading

    lines = []
    ready = threading.Event()

    def reader():
        with client.stream("GET", "/api/pipeline/batches/hb/events") as r:
            ready.set()
            count = 0
            for line in r.iter_lines():
                line = line.strip()
                lines.append(line)
                if line.startswith(": "):
                    count += 1
                    if count >= 1:
                        break

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    ready.wait(timeout=2)

    import time
    time.sleep(6)  # 等一个心跳间隔
    t.join(timeout=8)
    assert any(": heartbeat" in l for l in lines)
