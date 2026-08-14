"""
媒体处理 API
- POST /api/merge   多视频合并（纯拼接 / xfade 转场）
- POST /api/dub     视频配音 + 字幕（edge-tts + 烧录）
"""
import re
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException

from pipeline import merge as merge_pipeline
from web import settings

router = APIRouter(tags=["media"])


def _load_config() -> dict:
    if settings.CONFIG_PATH.exists():
        with open(settings.CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _resolve_video(path: str) -> Path:
    """把 /api/videos 返回的相对路径解析为绝对路径"""
    file_path = (settings.OUTPUT_DIR / path).resolve()
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, f"视频不存在: {path}")
    return file_path


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip())
    return name or datetime.now().strftime("%Y%m%d_%H%M%S")


@router.post("/merge")
async def merge_videos(body: dict):
    """
    合并多个视频
    body: {"video_paths": ["batch_x/shots/a.mp4", ...], "name": "可选",
           "mode": "concat" | "transition", "transition_duration": 0.5}
    """
    paths = (body or {}).get("video_paths") or []
    if not paths:
        raise HTTPException(400, "请选择至少一个视频")
    if len(paths) < 2:
        raise HTTPException(400, "合并需要至少 2 个视频")

    videos = [_resolve_video(p) for p in paths]
    mode = (body or {}).get("mode", "concat")
    name = _safe_name((body or {}).get("name", "merged"))
    cfg = _load_config()
    ffmpeg_cfg = cfg.get("ffmpeg", {})
    merge_cfg = cfg.get("merge", {})

    out_dir = settings.OUTPUT_DIR / "merged"
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(out_dir / f"{name}.mp4")

    if mode == "transition":
        dur = float((body or {}).get("transition_duration", merge_cfg.get("transition_duration", 0.5)))
        result = merge_pipeline.merge_with_transition(
            [str(v) for v in videos], output_path,
            transition_duration=dur,
            transition=merge_cfg.get("transition", "fade"),
            silent_sample_rate=int(merge_cfg.get("silent_sample_rate", 44100)),
            ffmpeg_crf=int(ffmpeg_cfg.get("crf", 13)),
            ffmpeg_pix_fmt=ffmpeg_cfg.get("pix_fmt", "yuv420p"),
            audio_codec=ffmpeg_cfg.get("audio_codec", "aac"),
            audio_bitrate=ffmpeg_cfg.get("audio_bitrate", "192k"),
        )
    else:
        result = merge_pipeline.merge_only(
            [str(v) for v in videos], output_path,
            silent_sample_rate=int(merge_cfg.get("silent_sample_rate", 44100)),
            ffmpeg_crf=int(ffmpeg_cfg.get("crf", 13)),
            ffmpeg_pix_fmt=ffmpeg_cfg.get("pix_fmt", "yuv420p"),
        )

    rel = Path(result).relative_to(settings.OUTPUT_DIR).as_posix()
    return {"ok": True, "message": "合并完成", "path": rel}


@router.post("/dub")
async def dub_video(body: dict):
    """
    视频配音 + 烧录字幕
    body: {"video_path": "...", "script": "台词文本", "voice": "可选", "rate": "可选"}
    """
    path = (body or {}).get("video_path", "")
    if not path:
        raise HTTPException(400, "请选择视频")
    video_path = _resolve_video(path)

    script = ((body or {}).get("script") or "").strip()
    if not script:
        raise HTTPException(400, "台词文本不能为空")

    cfg = _load_config()
    tts_cfg = cfg.get("tts", {})
    ffmpeg_cfg = cfg.get("ffmpeg", {})
    voice = (body or {}).get("voice") or tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural")
    rate = (body or {}).get("rate") or tts_cfg.get("rate", "+0%")

    name = _safe_name(Path(path).stem) + "_dubbed"
    out_dir = settings.OUTPUT_DIR / "dubbed"
    work_dir = settings.OUTPUT_DIR / "dubbed" / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    audio_path, srt_path = merge_pipeline.generate_tts(script, str(work_dir / name), voice=voice, rate=rate)

    total_dur = merge_pipeline.get_video_duration(str(video_path))
    tts_dur = merge_pipeline.get_video_duration(audio_path) if Path(audio_path).stat().st_size > 1000 else total_dur
    video_pad = max(0.0, tts_dur - total_dur)
    audio_pad = max(0.0, total_dur - tts_dur)

    output_path = str(out_dir / f"{name}.mp4")
    sub_cfg = ffmpeg_cfg.get("subtitle", {}) or {}
    merge_pipeline.compose_final(
        str(video_path), audio_path, srt_path, output_path,
        video_pad=video_pad, audio_pad=audio_pad,
        ffmpeg_crf=int(ffmpeg_cfg.get("crf", 13)),
        ffmpeg_pix_fmt=ffmpeg_cfg.get("pix_fmt", "yuv420p"),
        font_color=sub_cfg.get("font_color", "FFFFFF"),
        outline_color=sub_cfg.get("outline_color", "000000"),
        outline_width=int(sub_cfg.get("outline_width", 2)),
        alignment=int(sub_cfg.get("alignment", 2)),
        margin_v=int(sub_cfg.get("margin_v", 40)),
        shadow=int(sub_cfg.get("shadow", 0)),
    )

    rel = Path(output_path).relative_to(settings.OUTPUT_DIR).as_posix()
    return {"ok": True, "message": "配音字幕完成", "path": rel}
