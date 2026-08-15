"""Tracker 单元测试：单例、状态机、pub/sub"""
import asyncio
import pytest

from pipeline.tracker import Tracker, ShotState, BatchState, get_tracker


@pytest.fixture
def tracker():
    """每个测试一个新 tracker（不污染单例）"""
    return Tracker()


def test_tracker_starts_empty(tracker):
    assert tracker.list_batches() == []
    assert tracker.list_batches_with_meta() == []


def test_ensure_batch_creates_and_returns(tracker):
    b = tracker.ensure_batch("batch_x")
    assert isinstance(b, BatchState)
    assert b.batch_id == "batch_x"
    assert b.shots == {}
    # 再调用返回同一对象
    b2 = tracker.ensure_batch("batch_x")
    assert b2 is b


def test_ensure_batch_separates_ids(tracker):
    b1 = tracker.ensure_batch("batch_a")
    b2 = tracker.ensure_batch("batch_b")
    assert b1 is not b2
    assert {b.batch_id for b in [b1, b2]} == {"batch_a", "batch_b"}


def test_update_shot_creates_state(tracker):
    shot = tracker.update_shot(
        "batch_x", "1",
        status="done", stage="review",
        video_path="/tmp/a.mp4",
        table_id="t1", shot_id="s1",
        scene_desc="城市夜景", duration=5,
    )
    assert isinstance(shot, ShotState)
    assert shot.status == "done"
    assert shot.stage == "review"
    assert shot.video_path == "/tmp/a.mp4"
    assert shot.started_at > 0
    # 出现在 list_shots 里
    shots = tracker.list_shots("batch_x")
    assert len(shots) == 1
    assert shots[0].key == "1"


def test_update_shot_merges_fields(tracker):
    tracker.update_shot("batch_x", "1", status="analyzing")
    tracker.update_shot("batch_x", "1", stage="select")
    tracker.update_shot("batch_x", "1", status="submitting", stage="submit")
    shots = tracker.list_shots("batch_x")
    assert shots[0].status == "submitting"
    assert shots[0].stage == "submit"
    assert shots[0].finished_at is None  # 没标完成


def test_update_shot_records_finished_at_on_terminal(tracker):
    tracker.update_shot("batch_x", "1", status="done")
    s = tracker.list_shots("batch_x")[0]
    assert s.finished_at is not None
    assert s.finished_at >= s.started_at


def test_update_shot_records_error(tracker):
    tracker.update_shot("batch_x", "1", status="failed", error="comfyui down")
    s = tracker.list_shots("batch_x")[0]
    assert s.status == "failed"
    assert s.error == "comfyui down"


def test_confirm_and_unconfirm(tracker):
    tracker.update_shot("batch_x", "1", status="done", table_id="t1", shot_id="s1")
    tracker.mark_confirmed("batch_x", "1")
    s = tracker.list_shots("batch_x")[0]
    assert s.status == "confirmed"

    tracker.mark_unconfirmed("batch_x", "1")
    s = tracker.list_shots("batch_x")[0]
    assert s.status == "done"


def test_confirmed_filter(tracker):
    tracker.update_shot("batch_x", "1", status="done", table_id="t1", shot_id="s1")
    tracker.update_shot("batch_x", "2", status="done", table_id="t1", shot_id="s2")
    tracker.update_shot("batch_x", "3", status="failed", table_id="t1", shot_id="s3")
    tracker.mark_confirmed("batch_x", "1")
    confirmed = tracker.list_shots("batch_x", status_filter="confirmed")
    assert {s.key for s in confirmed} == {"1"}


def test_status_filter(tracker):
    tracker.update_shot("batch_x", "1", status="done")
    tracker.update_shot("batch_x", "2", status="failed")
    tracker.update_shot("batch_x", "3", status="done")
    done = tracker.list_shots("batch_x", status_filter="done")
    assert {s.key for s in done} == {"1", "3"}


@pytest.mark.asyncio
async def test_subscribe_receives_updates(tracker):
    queue = tracker.subscribe("batch_x")
    assert queue in tracker.queues["batch_x"]

    tracker.update_shot("batch_x", "1", status="done", stage="review")
    # 等异步推送
    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event["type"] == "shot_update"
    assert event["batch_id"] == "batch_x"
    assert event["shot_key"] == "1"
    assert event["fields"]["status"] == "done"


@pytest.mark.asyncio
async def test_unsubscribe(tracker):
    q1 = tracker.subscribe("batch_x")
    q2 = tracker.subscribe("batch_x")
    tracker.unsubscribe("batch_x", q1)
    assert q1 not in tracker.queues["batch_x"]
    assert q2 in tracker.queues["batch_x"]


@pytest.mark.asyncio
async def test_queue_full_drops_oldest(tracker):
    """满队列策略：丢最老事件，防止内存膨胀"""
    tracker.max_queue_size = 3
    queue = tracker.subscribe("batch_x")
    # 推 5 个事件
    for i in range(5):
        tracker.update_shot("batch_x", str(i), status="analyzing")
    # 取 3 次：应得到 i=2,3,4
    events = []
    for _ in range(3):
        events.append(await asyncio.wait_for(queue.get(), timeout=1))
    statuses = [e["fields"]["status"] for e in events]
    assert statuses == ["analyzing", "analyzing", "analyzing"]
    # 检查 shot_key 确实是 2,3,4
    keys = [e["shot_key"] for e in events]
    assert keys == ["2", "3", "4"]


@pytest.mark.asyncio
async def test_batch_done_event(tracker):
    queue = tracker.subscribe("batch_x")
    tracker.ensure_batch("batch_x")
    tracker.mark_done("batch_x", exit_code=0)
    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event["type"] == "batch_done"
    assert event["batch_id"] == "batch_x"
    assert event["exit_code"] == 0


def test_get_tracker_returns_singleton():
    t1 = get_tracker()
    t2 = get_tracker()
    assert t1 is t2


def test_reset_clears_state():
    """供测试 / reload 用"""
    t = get_tracker()
    t.ensure_batch("batch_x")
    t.update_shot("batch_x", "1", status="done")
    t.reset()
    assert t.list_batches() == []


def test_to_dict_for_api(tracker):
    tracker.update_shot("batch_x", "1", status="done", table_id="t1", shot_id="s1",
                        scene_desc="test", duration=3)
    s = tracker.list_shots("batch_x")[0]
    d = s.to_dict()
    assert d["key"] == "1"
    assert d["status"] == "done"
    assert d["table_id"] == "t1"
    assert d["shot_id"] == "s1"
    assert d["scene_desc"] == "test"
    assert d["duration"] == 3
    assert "started_at" in d