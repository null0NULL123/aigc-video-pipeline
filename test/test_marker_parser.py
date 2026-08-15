"""marker 行解析 + tracker 更新端到端测试"""
import json
import subprocess
import sys
from pathlib import Path

from pipeline.messages import Msg
from pipeline.tracker import Tracker, get_tracker, reset_tracker
from web.routers.pipeline import _parse_marker_line, _handle_marker


def test_parse_marker_line_valid():
    line = f'{Msg.PIPE_EVENT_PREFIX} {json.dumps({"event": "shot_done", "batch_id": "b1", "shot_key": "1", "status": "done", "video_path": "/tmp/x.mp4"})}'
    p = _parse_marker_line(line)
    assert p is not None
    assert p["event"] == "shot_done"
    assert p["batch_id"] == "b1"
    assert p["shot_key"] == "1"
    assert p["video_path"] == "/tmp/x.mp4"


def test_parse_marker_line_non_marker():
    assert _parse_marker_line("普通日志行") is None
    assert _parse_marker_line("") is None
    assert _parse_marker_line(f"{Msg.PIPE_EVENT_PREFIX} not-json") is None


def test_handle_marker_creates_shot():
    reset_tracker()
    tracker = get_tracker()
    payload = {
        "event": "shot_start",
        "batch_id": "b1",
        "shot_key": "1",
        "status": "analyzing",
        "scene_desc": "城市夜景",
        "duration": 5,
    }
    _handle_marker(payload)
    shot = tracker.get_shot("b1", "1")
    assert shot is not None
    assert shot.status == "analyzing"
    assert shot.scene_desc == "城市夜景"
    assert shot.duration == 5


def test_handle_marker_batch_done():
    reset_tracker()
    tracker = get_tracker()
    payload = {"event": "batch_done", "batch_id": "b1", "exit_code": 0}
    _handle_marker(payload)
    batch = tracker.get_batch("b1")
    assert batch is not None
    assert batch.exit_code == 0
    assert batch.finished_at is not None


def test_handle_marker_ignores_empty_batch():
    """没有 batch_id 时不应崩"""
    payload = {"event": "shot_start", "shot_key": "1"}
    _handle_marker(payload)  # 不抛异常


# ---- 端到端：用 python -c 跑 emit_event，解析它的 stdout ----

def test_subprocess_marker_end_to_end(tmp_path: Path):
    """真实子进程输出 marker → 解析 → tracker 更新"""
    reset_tracker()
    # 跑一个 python 脚本，emit 几条 marker 然后退出
    script = """
import sys
sys.path.insert(0, '{cwd}')
from pipeline.generator import set_current_batch_id, emit_event
set_current_batch_id('e2e_batch')
emit_event('shot_start', shot_key='1', status='analyzing', scene_desc='demo', duration=4)
emit_event('shot_progress', shot_key='1', status='submitting')
emit_event('shot_done', shot_key='1', status='done', stage='done', video_path='/tmp/out.mp4')
emit_event('shot_start', shot_key='2', status='analyzing', scene_desc='demo2', duration=4)
emit_event('shot_failed', shot_key='2', status='failed', stage='validate', error='bad prompt')
emit_event('batch_done', shot_key='', exit_code=0, done=1, failed=1, pending_ffmpeg=0)
""".format(cwd=str(Path.cwd()).replace("\\\\", "\\\\\\\\"))

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(Path.cwd()),
    )
    assert proc.returncode == 0

    # 现在模拟 pipeline.py 的 reader：解析每行 marker
    for line in proc.stdout.splitlines():
        payload = _parse_marker_line(line)
        if payload:
            _handle_marker(payload)

    tracker = get_tracker()
    shots = tracker.list_shots("e2e_batch")
    assert len(shots) == 2
    s1 = tracker.get_shot("e2e_batch", "1")
    s2 = tracker.get_shot("e2e_batch", "2")
    assert s1.status == "done"
    assert s1.video_path == "/tmp/out.mp4"
    assert s1.scene_desc == "demo"
    assert s2.status == "failed"
    assert s2.error == "bad prompt"

    batch = tracker.get_batch("e2e_batch")
    assert batch.exit_code == 0
    assert batch.finished_at is not None