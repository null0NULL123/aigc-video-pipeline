"""合并 / 配音字幕 API 测试（monkeypatch 掉真实 ffmpeg 调用）"""
import io


def _seed_videos(client, workdir):
    out = workdir / "output"
    (out / "batch_x" / "shots").mkdir(parents=True)
    for n in ("1", "2"):
        (out / "batch_x" / "shots" / f"batch_x_shot_{n}.mp4").write_bytes(b"v" + n.encode())
    return out


# ── 合并 ─────────────────────────────────────────────────

def test_merge_concat(client, workdir, monkeypatch):
    _seed_videos(client, workdir)
    calls = {}

    def fake_merge_only(paths, output_path, **kw):
        calls["merge_only"] = (paths, output_path)
        return output_path

    monkeypatch.setattr("web.routers.media.merge_pipeline.merge_only", fake_merge_only)

    r = client.post("/api/merge", json={
        "video_paths": ["batch_x/shots/batch_x_shot_1.mp4", "batch_x/shots/batch_x_shot_2.mp4"],
        "name": "demo", "mode": "concat",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # 新行为：手动合并加 manual_ 前缀 + 时间戳避免冲突
    import re as _re
    assert _re.match(r"^merged/manual_\d{8}_\d{6}_demo\.mp4$", r.json()["path"]), \
        f"unexpected path: {r.json()['path']}"
    assert len(calls["merge_only"][0]) == 2


def test_merge_transition(client, workdir, monkeypatch):
    _seed_videos(client, workdir)
    calls = {}

    def fake_transition(paths, output_path, **kw):
        calls["kw"] = kw
        return output_path

    monkeypatch.setattr("web.routers.media.merge_pipeline.merge_with_transition", fake_transition)

    r = client.post("/api/merge", json={
        "video_paths": ["batch_x/shots/batch_x_shot_1.mp4", "batch_x/shots/batch_x_shot_2.mp4"],
        "name": "demo", "mode": "transition", "transition_duration": 1.0,
    })
    assert r.status_code == 200
    assert calls["kw"]["transition_duration"] == 1.0


def test_merge_errors(client, workdir):
    _seed_videos(client, workdir)
    assert client.post("/api/merge", json={"video_paths": [], "name": "x"}).status_code == 400
    assert client.post("/api/merge", json={"video_paths": ["batch_x/shots/batch_x_shot_1.mp4"]}).status_code == 400
    r = client.post("/api/merge", json={"video_paths": ["no_such.mp4", "batch_x/shots/batch_x_shot_1.mp4"]})
    assert r.status_code == 404


# ── 配音字幕 ─────────────────────────────────────────────

def test_dub(client, workdir, monkeypatch):
    _seed_videos(client, workdir)
    calls = {}

    def fake_generate_tts(script, out, voice=None, rate=None, **kw):
        calls.update(script=script, voice=voice, rate=rate)
        audio = workdir / "a.mp3"
        audio.write_bytes(b"x" * 2000)
        return (str(audio), str(workdir / "a.srt"))

    monkeypatch.setattr("web.routers.media.merge_pipeline.generate_tts", fake_generate_tts)
    monkeypatch.setattr("web.routers.media.merge_pipeline.get_video_duration", lambda p: 5.0)
    monkeypatch.setattr("web.routers.media.merge_pipeline.compose_final",
                        lambda *a, **kw: calls.update(composed=True) or str(workdir / "output" / "dubbed" / "x_dubbed.mp4"))

    r = client.post("/api/dub", json={
        "video_path": "batch_x/shots/batch_x_shot_1.mp4",
        "script": "你好，世界", "voice": "zh-CN-YunxiNeural", "rate": "+10%",
    })
    assert r.status_code == 200
    assert calls["script"] == "你好，世界"
    assert calls["voice"] == "zh-CN-YunxiNeural"
    assert calls["composed"] is True


def test_dub_missing_fields(client, workdir):
    _seed_videos(client, workdir)
    assert client.post("/api/dub", json={"video_path": "batch_x/shots/batch_x_shot_1.mp4"}).status_code == 400
    assert client.post("/api/dub", json={"script": "x"}).status_code == 400