"""合并模块测试：字幕样式 / per-shot 配音模式"""
from pipeline import merge as mp


def test_to_bgr():
    assert mp._to_bgr("FFFFFF") == "FFFFFF"
    assert mp._to_bgr("112233") == "332211"
    assert mp._to_bgr("#AABBCC") == "CCBBAA"
    assert mp._to_bgr("bad") == "FFFFFF"


def test_build_force_style():
    style = mp.build_force_style(
        font_family="Microsoft YaHei", font_size=48, font_color="FFFFFF",
        outline_color="000000", outline_width=2, alignment=2, margin_v=40, shadow=1,
    )
    assert "FontName=Microsoft YaHei" in style
    assert "FontSize=48" in style
    assert "PrimaryColour=&H00FFFFFF" in style
    assert "OutlineColour=&H00000000" in style
    assert "Outline=2" in style
    assert "Alignment=2" in style
    assert "MarginV=40" in style
    assert "Shadow=1" in style
    assert ":" not in style  # 避免 ffmpeg filter 冒号转义问题


def test_build_force_style_no_shadow():
    style = mp.build_force_style(shadow=0)
    assert "Shadow" not in style


def _fake_shot_results():
    return [
        {"shot_id": "1", "status": "done", "video_path": "/v/1.mp4", "dialogue": "台词一", "screen_text": ""},
        {"shot_id": "2", "status": "done", "video_path": "/v/2.mp4", "dialogue": "", "screen_text": ""},
        {"shot_id": "3", "status": "failed", "video_path": "", "dialogue": "", "screen_text": ""},
    ]


def _cfg(**overrides):
    cfg = {
        "_batch_id": "batch_t",
        "output": {
            "audio_dir": "output/batch_t/audio", "subs_dir": "output/batch_t/subs",
            "merged_dir": "output/batch_t/merged", "final_dir": "output/batch_t/final",
        },
        "tts": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%"},
        "ffmpeg": {"crf": 13, "pix_fmt": "yuv420p", "font_family": "Microsoft YaHei", "font_size": 48,
                   "subtitle": {"font_color": "FFFF00", "outline_color": "0000FF",
                                "outline_width": 3, "alignment": 8, "margin_v": 20, "shadow": 2}},
        "merge": {"tts_mode": "per_shot", "break_between_shots_ms": 600, "silent_sample_rate": 44100},
    }
    cfg.update(overrides)
    return cfg


def test_run_per_shot(monkeypatch, tmp_path):
    """per-shot 模式：每个有台词的镜头独立 TTS+合成，再拼接"""
    import os
    from pathlib import Path

    shots_dir = tmp_path / "v"
    shots_dir.mkdir()
    v1, v2 = shots_dir / "1.mp4", shots_dir / "2.mp4"
    v1.write_bytes(b"v1")
    v2.write_bytes(b"v2")

    results = [
        {"shot_id": "1", "status": "done", "video_path": str(v1), "dialogue": "台词一", "screen_text": ""},
        {"shot_id": "2", "status": "done", "video_path": str(v2), "dialogue": "", "screen_text": ""},
    ]

    calls = {"tts": [], "composed": [], "concat": None, "normalized": []}

    def fake_tts(text, out, voice=None, rate=None, fallback_duration=1.0):
        calls["tts"].append(text)
        audio = Path(out).with_suffix(".mp3")
        audio.write_bytes(b"x" * 2000)
        srt = Path(out).with_suffix(".srt")
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
        return str(audio), str(srt)

    def fake_compose(*a, **kw):
        calls["composed"].append((a[0], kw.get("font_color"), kw.get("shadow")))
        out = Path(a[3])
        out.write_bytes(b"comp")
        return str(out)

    def fake_concat(video_list, out, *a, **kw):
        calls["concat"] = video_list
        Path(out).write_bytes(b"final")
        return str(out)

    def fake_normalize(video_path, out, *a, **kw):
        calls["normalized"].append(video_path)
        Path(out).write_bytes(b"norm")
        return str(out)

    def fake_duration(path):
        return 5.0

    monkeypatch.setattr(mp, "generate_tts", fake_tts)
    monkeypatch.setattr(mp, "compose_final", fake_compose)
    monkeypatch.setattr(mp, "concat_videos", fake_concat)
    monkeypatch.setattr(mp, "normalize_segment", fake_normalize)
    monkeypatch.setattr(mp, "get_video_duration", fake_duration)

    # 输出目录重定向到临时目录
    cfg = _cfg()
    base = tmp_path / "out"
    for k in ("audio_dir", "subs_dir", "merged_dir", "final_dir"):
        cfg["output"][k] = str(base / k)

    result = mp.run(results, cfg)
    assert result.endswith("batch_t.mp4")

    # 只有有台词的镜头触发 TTS
    assert calls["tts"] == ["台词一"]
    # 合成调用带上字幕样式
    assert calls["composed"][0][1] == "FFFF00"
    assert calls["composed"][0][2] == 2
    # 拼接顺序 = 镜头1(合成) + 镜头2(仅归一化)
    assert len(calls["concat"]) == 2


def test_run_whole_passes_subtitle_style(monkeypatch, tmp_path):
    """whole 模式：compose_final 收到字幕样式参数"""
    from pathlib import Path

    shots_dir = tmp_path / "v"
    shots_dir.mkdir()
    v1 = shots_dir / "1.mp4"
    v1.write_bytes(b"v1")

    results = [
        {"shot_id": "1", "status": "done", "video_path": str(v1), "dialogue": "台词", "screen_text": ""},
    ]

    captured = {}

    def fake_normalize(video_path, out, *a, **kw):
        Path(out).write_bytes(b"norm")
        return str(out)

    def fake_concat(video_list, out, *a, **kw):
        Path(out).write_bytes(b"merged")
        return str(out)

    def fake_tts(text, out, voice=None, rate=None, fallback_duration=1.0):
        audio = Path(out).with_suffix(".mp3")
        audio.write_bytes(b"x" * 2000)
        srt = Path(out).with_suffix(".srt")
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
        return str(audio), str(srt)

    def fake_compose(*a, **kw):
        captured.update(kw)
        Path(a[3]).write_bytes(b"final")
        return str(a[3])

    monkeypatch.setattr(mp, "generate_tts", fake_tts)
    monkeypatch.setattr(mp, "compose_final", fake_compose)
    monkeypatch.setattr(mp, "concat_videos", fake_concat)
    monkeypatch.setattr(mp, "normalize_segment", fake_normalize)
    monkeypatch.setattr(mp, "get_video_duration", lambda p: 5.0)

    cfg = _cfg(merge={"tts_mode": "whole", "break_between_shots_ms": 600, "silent_sample_rate": 44100})
    base = tmp_path / "out"
    for k in ("audio_dir", "subs_dir", "merged_dir", "final_dir"):
        cfg["output"][k] = str(base / k)

    mp.run(results, cfg)
    assert captured["font_color"] == "FFFF00"
    assert captured["outline_width"] == 3
    assert captured["alignment"] == 8
    assert captured["margin_v"] == 20
    assert captured["shadow"] == 2


def test_run_whole_uses_transition_when_configured(monkeypatch, tmp_path):
    """whole 模式 + transition=fade：用 merge_with_transition 拼接"""
    from pathlib import Path

    shots_dir = tmp_path / "v"
    shots_dir.mkdir()
    v1, v2 = shots_dir / "1.mp4", shots_dir / "2.mp4"
    v1.write_bytes(b"v1")
    v2.write_bytes(b"v2")

    results = [
        {"shot_id": "1", "status": "done", "video_path": str(v1), "dialogue": "", "screen_text": ""},
        {"shot_id": "2", "status": "done", "video_path": str(v2), "dialogue": "", "screen_text": ""},
    ]

    calls = {"transition": None, "concat": None}

    def fake_normalize(video_path, out, *a, **kw):
        Path(out).write_bytes(b"norm")
        return str(out)

    def fake_transition(video_list, out, **kw):
        calls["transition"] = kw
        Path(out).write_bytes(b"merged")
        return str(out)

    def fake_concat(video_list, out, *a, **kw):
        calls["concat"] = video_list
        Path(out).write_bytes(b"merged")
        return str(out)

    def fake_tts(text, out, voice=None, rate=None, fallback_duration=1.0):
        srt = Path(out).with_suffix(".srt")
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
        return "", str(srt)

    monkeypatch.setattr(mp, "generate_tts", fake_tts)
    monkeypatch.setattr(mp, "concat_videos", fake_concat)
    monkeypatch.setattr(mp, "merge_with_transition", fake_transition)
    monkeypatch.setattr(mp, "normalize_segment", fake_normalize)
    monkeypatch.setattr(mp, "get_video_duration", lambda p: 5.0)

    cfg = _cfg(merge={"tts_mode": "none", "transition": "fade", "transition_duration": 0.8,
                      "break_between_shots_ms": 600, "silent_sample_rate": 44100})
    base = tmp_path / "out"
    for k in ("audio_dir", "subs_dir", "merged_dir", "final_dir"):
        cfg["output"][k] = str(base / k)

    mp.run(results, cfg)
    assert calls["transition"] is not None
    assert calls["transition"]["transition"] == "fade"
    assert calls["transition"]["transition_duration"] == 0.8
    assert calls["concat"] is None


def test_run_whole_plain_concat_without_transition(monkeypatch, tmp_path):
    """whole 模式 + 无 transition 配置：仍走 concat_videos"""
    from pathlib import Path

    shots_dir = tmp_path / "v"
    shots_dir.mkdir()
    v1, v2 = shots_dir / "1.mp4", shots_dir / "2.mp4"
    v1.write_bytes(b"v1")
    v2.write_bytes(b"v2")

    results = [
        {"shot_id": "1", "status": "done", "video_path": str(v1), "dialogue": "", "screen_text": ""},
        {"shot_id": "2", "status": "done", "video_path": str(v2), "dialogue": "", "screen_text": ""},
    ]

    calls = {"transition": None, "concat": None}

    def fake_normalize(video_path, out, *a, **kw):
        Path(out).write_bytes(b"norm")
        return str(out)

    def fake_transition(video_list, out, **kw):
        calls["transition"] = kw
        Path(out).write_bytes(b"merged")
        return str(out)

    def fake_concat(video_list, out, *a, **kw):
        calls["concat"] = video_list
        Path(out).write_bytes(b"merged")
        return str(out)

    monkeypatch.setattr(mp, "concat_videos", fake_concat)
    monkeypatch.setattr(mp, "merge_with_transition", fake_transition)
    monkeypatch.setattr(mp, "normalize_segment", fake_normalize)
    monkeypatch.setattr(mp, "get_video_duration", lambda p: 5.0)

    cfg = _cfg(merge={"tts_mode": "none", "transition": "none",
                      "break_between_shots_ms": 600, "silent_sample_rate": 44100})
    base = tmp_path / "out"
    for k in ("audio_dir", "subs_dir", "merged_dir", "final_dir"):
        cfg["output"][k] = str(base / k)

    mp.run(results, cfg)
    assert calls["concat"] is not None
    assert calls["transition"] is None


def test_get_video_stream_duration_uses_video_stream(monkeypatch):
    """get_video_stream_duration 探测视频流时长而非容器时长"""
    import subprocess

    def fake_run(cmd, capture_output=True, text=True):
        class R:
            stdout = "4.041667\n"
            stderr = ""
            returncode = 0
        return R()

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    assert mp.get_video_stream_duration("x.mp4") == 4.041667


def test_get_video_resolution_parses(monkeypatch):
    """get_video_resolution 解析 width,height"""
    def fake_run(cmd, capture_output=True, text=True):
        class R:
            stdout = "1280,720\n"
            stderr = ""
            returncode = 0
        return R()

    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    assert mp.get_video_resolution("x.mp4") == (1280, 720)


def test_merge_with_transition_unifies_resolution(monkeypatch, tmp_path):
    """转场拼接时不同分辨率镜头先统一再 xfade"""
    from pathlib import Path

    shots_dir = tmp_path / "v"
    shots_dir.mkdir()
    v1, v2 = shots_dir / "1.mp4", shots_dir / "2.mp4"
    v1.write_bytes(b"v1")
    v2.write_bytes(b"v2")

    calls = {"norm": [], "uni": [], "x": None}
    sizes = iter([(960, 960), (1280, 720)])

    def fake_normalize(video_path, out, *a, **kw):
        calls["norm"].append((video_path, str(out)))
        Path(out).write_bytes(b"n")
        return str(out)

    def fake_resolution(p):
        return next(sizes)

    def fake_uniformize(p, out, width, height):
        calls["uni"].append((p, width, height))
        Path(out).write_bytes(b"u")
        return str(out)

    def fake_duration(p):
        return 4.0

    def fake_run(cmd, capture_output=True, text=True):
        calls["x"] = cmd
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(mp, "normalize_segment", fake_normalize)
    monkeypatch.setattr(mp, "get_video_resolution", fake_resolution)
    monkeypatch.setattr(mp, "uniformize_resolution", fake_uniformize)
    monkeypatch.setattr(mp, "get_video_stream_duration", fake_duration)
    monkeypatch.setattr(mp.subprocess, "run", fake_run)

    out = str(tmp_path / "out.mp4")
    mp.merge_with_transition([str(v1), str(v2)], out, transition_duration=0.5, transition="fade")

    # 归一化两个输入
    assert len(calls["norm"]) == 2
    # 分辨率不同的段被统一到基准 1280x720
    assert len(calls["uni"]) == 1
    assert calls["uni"][0][1:] == (1280, 720)
    # 生成 xfade filter
    assert "xfade" in str(calls["x"])
    assert "acrossfade" in str(calls["x"])