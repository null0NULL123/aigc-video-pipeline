"""
镜头合并
先拼接所有镜头视频 → 再对整个合并视频做全局 TTS + 字幕 → 最终一次合成
"""
import subprocess
import shutil
import os
from pathlib import Path

from pipeline.log import get_logger
from pipeline.messages import Msg

log = get_logger("merge")


def get_video_duration(video_path: str) -> float:
    """ffprobe 获取视频/音频时长（秒）"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0", video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def has_audio_track(video_path: str) -> bool:
    """检测视频是否带音轨"""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0", video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() != ""


def generate_tts(text: str, output_path: str,
                 voice: str = "zh-CN-XiaoxiaoNeural",
                 rate: str = "+0%",
                 fallback_duration: float = 1.0) -> tuple[str, str]:
    """
    edge-tts 生成语音 + SRT 字幕（时间轴为全局，从 0 开始）
    Returns: (audio_path, srt_path)
    """
    out = Path(output_path)
    audio_path = out.with_suffix(".mp3")
    srt_path = out.with_suffix(".srt")

    cmd = [
        "edge-tts",
        "--voice", voice,
        "--text", text,
        "--write-media", str(audio_path),
        "--write-subtitles", str(srt_path),
        "--rate", rate,
    ]
    log.info(Msg.MERGE_TTS.format(text=text[:30]))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(Msg.MERGE_ERR_TTS.format(err=result.stderr[:200]))
        _create_silent_audio(audio_path, fallback_duration)
        _create_empty_srt(srt_path, text)
    else:
        log.info(Msg.MERGE_TTS_OK.format(file=audio_path.name))

    return str(audio_path), str(srt_path)


def _create_silent_audio(path: Path, duration: float = 1.0):
    """ffmpeg 生成静音音频"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", str(duration),
        "-c:a", "aac", "-b:a", "192k",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True)


def _create_empty_srt(path: Path, text: str = ""):
    """生成简单 SRT"""
    content = text if text else "(无台词)"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"1\n00:00:00,000 --> 00:00:05,000\n{content}\n")


def _clean_break_entries(srt_path: str):
    """移除 edge-tts 把 <break> 标签当作词条写进 SRT 的字幕块（停顿不需要字幕）"""
    p = Path(srt_path)
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        content = f.read()
    blocks = content.split("\n\n")
    kept = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        text_lines = [ln for ln in lines[2:] if "-->" not in ln]
        if any("break" in ln for ln in text_lines):
            continue
        lines[0] = str(len(kept) + 1)
        kept.append("\n".join(lines))
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n\n".join(kept) + "\n")


def normalize_segment(video_path: str, output_path: str,
                      silent_sample_rate: int = 44100) -> str:
    """
    归一化单段镜头：无音轨的补静音 AAC 轨，保证 concat 轨道布局一致
    已有音轨的直接复制，不重新编码
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if has_audio_track(video_path):
        shutil.copy2(video_path, out)
        return str(out)

    log.info(Msg.MERGE_NORMALIZE.format(file=Path(video_path).name))
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-f", "lavfi", "-i", f"anullsrc=r={silent_sample_rate}:cl=mono",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out),
    ]
    subprocess.run(cmd, capture_output=True, text=True)
    return str(out)


def build_global_script(shot_results: list[dict], break_ms: int = 600) -> str:
    """
    按镜头顺序拼接台词为一段全局脚本（供一次 TTS）
    镜头间用 <break> 停顿分隔；无台词镜头只留停顿占位
    """
    parts = []
    for r in shot_results:
        dialogue = (r.get("dialogue") or "").strip()
        screen_text = (r.get("screen_text") or "").strip()
        text = dialogue if dialogue else screen_text
        if text:
            parts.append(text)
    if not parts:
        return ""
    if break_ms > 0:
        sep = f"\n<break time=\"{break_ms}ms\"/>\n"
    else:
        sep = "\n"
    return sep.join(parts)


def _to_bgr(color: str) -> str:
    """CSS 十六进制颜色 RRGGBB → libass &H00BBGGRR"""
    c = (color or "").strip().lstrip("#").upper()
    if len(c) != 6:
        c = "FFFFFF"
    return f"{c[4:6]}{c[2:4]}{c[0:2]}"


def build_force_style(font_family: str = "Microsoft YaHei", font_size: int = 48,
                      font_color: str = "FFFFFF", outline_color: str = "000000",
                      outline_width: int = 2, alignment: int = 2,
                      margin_v: int = 40, shadow: int = 0) -> str:
    """构造 libass force_style（无冒号，避免 ffmpeg filter 转义问题）"""
    parts = [
        f"FontName={font_family}",
        f"FontSize={font_size}",
        f"PrimaryColour=&H00{_to_bgr(font_color)}",
        f"OutlineColour=&H00{_to_bgr(outline_color)}",
        f"Outline={int(outline_width)}",
        f"Alignment={int(alignment)}",
        f"MarginV={int(margin_v)}",
    ]
    if int(shadow) > 0:
        parts.append(f"Shadow={int(shadow)}")
    return ",".join(parts)


def compose_final(merged_path: str, audio_path: str, srt_path: str,
                  output_path: str, video_pad: float = 0.0,
                  audio_pad: float = 0.0, ffmpeg_crf: int = 13,
                  ffmpeg_pix_fmt: str = "yuv420p",
                  font_family: str = "Microsoft YaHei",
                  font_size: int = 48,
                  font_color: str = "FFFFFF", outline_color: str = "000000",
                  outline_width: int = 2, alignment: int = 2,
                  margin_v: int = 40, shadow: int = 0) -> str:
    """
    最终一次合成：合并视频 + 全局 TTS 音频（替换原声）+ 全局字幕烧录
    video_pad: 语音比视频长，视频末帧冻结补帧
    audio_pad: 语音比视频短，音频尾部补静音
    注：ffmpeg filter 的冒号转义在不同构建下不可靠，
        字幕用相对文件名 + force_style（无冒号），工作目录设为 SRT 所在目录，其余路径用绝对路径
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    has_audio = audio_path and Path(audio_path).exists() and Path(audio_path).stat().st_size > 1000
    has_srt = srt_path and Path(srt_path).exists() and Path(srt_path).stat().st_size > 10
    srt_name = Path(srt_path).name if has_srt else ""
    cwd = str(Path(srt_path).parent) if has_srt else None

    vf_parts = []
    af_parts = []
    if video_pad > 0:
        vf_parts.append(f"tpad=stop_mode=clone:stop_duration={video_pad:.3f}")
        if has_audio:
            af_parts.append(f"apad=pad_dur={video_pad:.3f}")
    if audio_pad > 0 and has_audio:
        af_parts.append(f"apad=pad_dur={audio_pad:.3f}")
    if has_srt:
        force_style = build_force_style(
            font_family=font_family, font_size=font_size, font_color=font_color,
            outline_color=outline_color, outline_width=outline_width,
            alignment=alignment, margin_v=margin_v, shadow=shadow,
        )
        vf_parts.append(f"subtitles={srt_name}:force_style='{force_style}'")

    cmd = ["ffmpeg", "-y", "-i", os.path.abspath(merged_path)]
    if has_audio:
        cmd += ["-i", os.path.abspath(audio_path)]
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    if af_parts:
        cmd += ["-af", ",".join(af_parts)]
    if has_audio or vf_parts or af_parts:
        cmd += ["-c:v", "libx264", "-profile:v", "main",
                "-crf", str(ffmpeg_crf), "-pix_fmt", ffmpeg_pix_fmt]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        if video_pad > 0 or audio_pad > 0:
            cmd += ["-shortest"]
    else:
        cmd += ["-c", "copy"]
    cmd += [os.path.abspath(str(out))]

    log.info(Msg.MERGE_FINAL.format(file=out.name))
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        log.error(Msg.MERGE_ERR_FINAL.format(err=result.stderr[-300:]))
        shutil.copy2(merged_path, out)
        log.info(f"保底复制: {out.name}")

    return str(out)


def concat_videos(video_list: list[str], output_path: str,
                  ffmpeg_crf: int = 13, ffmpeg_pix_fmt: str = "yuv420p") -> str:
    """
    多段视频拼接（不重新编码，最快）
    """
    if not video_list:
        raise ValueError("无视频可拼接")

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    filelist_path = out_path.parent / "_concat_list.txt"
    with open(filelist_path, "w", encoding="utf-8") as f:
        for vp in video_list:
            f.write(f"file '{Path(vp).resolve()}'\n")

    log.info(Msg.MERGE_CONCAT.format(count=len(video_list)))

    # 方式1: 不重新编码
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(filelist_path),
        "-c", "copy",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        log.info(Msg.MERGE_MERGED.format(path=str(out_path)))
        filelist_path.unlink(missing_ok=True)
        return str(out_path)

    # 方式2: 重新编码 fallback
    log.warning(Msg.MERGE_CONCAT_FALLBACK)
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(filelist_path),
        "-c:v", "libx264", "-profile:v", "main",
        "-crf", str(ffmpeg_crf), "-pix_fmt", ffmpeg_pix_fmt,
        "-c:a", "aac", "-b:a", "192k",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"拼接失败: {result.stderr[-300:]}")

    log.info(Msg.MERGE_MERGED.format(path=str(out_path)))
    filelist_path.unlink(missing_ok=True)
    return str(out_path)


def merge_only(video_paths: list[str], output_path: str,
               silent_sample_rate: int = 44100,
               ffmpeg_crf: int = 13, ffmpeg_pix_fmt: str = "yuv420p") -> str:
    """
    纯拼接多段视频（不配音、不加字幕）
    归一化每段（无音轨补静音）→ concat，优先 copy 不重新编码
    """
    if not video_paths:
        raise ValueError("无视频可拼接")
    if len(video_paths) == 1:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video_paths[0], out)
        log.info(f"单视频直接复制: {out.name}")
        return str(out)

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = []
    for i, vp in enumerate(video_paths):
        norm_path = str(out_path.parent / f"norm_{i}.mp4")
        normalized.append(normalize_segment(vp, norm_path, silent_sample_rate))
    return concat_videos(normalized, str(out_path), ffmpeg_crf, ffmpeg_pix_fmt)


def merge_with_transition(video_paths: list[str], output_path: str,
                          transition_duration: float = 0.5,
                          transition: str = "fade",
                          silent_sample_rate: int = 44100,
                          ffmpeg_crf: int = 13, ffmpeg_pix_fmt: str = "yuv420p",
                          audio_codec: str = "aac", audio_bitrate: str = "192k") -> str:
    """
    xfade 转场拼接（重新编码）
    多段视频带转场过渡，音频用 acrossfade 同步过渡
    """
    if len(video_paths) == 1:
        return merge_only(video_paths, output_path, silent_sample_rate, ffmpeg_crf, ffmpeg_pix_fmt)

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 归一化保证都有音轨，记录时长
    inputs = []
    durations = []
    for i, vp in enumerate(video_paths):
        norm_path = str(out_path.parent / f"xfade_norm_{i}.mp4")
        inputs.append(normalize_segment(vp, norm_path, silent_sample_rate))
        durations.append(get_video_duration(inputs[-1]))

    dur = max(0.05, float(transition_duration))
    n = len(inputs)

    # xfade offset 公式：第 k 段转场 offset = Σdurations[0..k] - (k+1)*dur
    vf_parts, af_parts = [], []
    prev_v, prev_a = "[0:v]", "[0:a]"
    acc = 0.0
    for k in range(1, n):
        acc += durations[k - 1]
        offset = acc - k * dur
        v_out = f"[v{k}]" if k < n - 1 else "[vout]"
        a_out = f"[a{k}]" if k < n - 1 else "[aout]"
        vf_parts.append(
            f"{prev_v}[{k}:v]xfade=transition={transition}:duration={dur:.3f}:offset={offset:.3f}{v_out}")
        af_parts.append(f"{prev_a}[{k}:a]acrossfade=d={dur:.3f}{a_out}")
        prev_v, prev_a = v_out, a_out

    filter_complex = ";".join(vf_parts + af_parts)

    cmd = ["ffmpeg", "-y"]
    for inp in inputs:
        cmd += ["-i", inp]
    cmd += ["-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-crf", str(ffmpeg_crf), "-pix_fmt", ffmpeg_pix_fmt,
            "-c:a", audio_codec, "-b:a", audio_bitrate,
            "-t", f"{sum(durations) - (n - 1) * dur:.3f}",
            str(out_path)]

    log.info(f"xfade 转场拼接 {n} 段（{transition} {dur}s）...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"转场拼接失败: {result.stderr[-300:]}")

    log.info(Msg.MERGE_MERGED.format(path=str(out_path)))
    return str(out_path)


def _compose_shot_segment(video_path: str, sid: str, script: str,
                          audio_dir: Path, merged_dir: Path,
                          voice: str, rate: str, silent_rate: int,
                          crf: int, pix_fmt: str, font_family: str, font_size: int,
                          sub_style: dict) -> str:
    """单镜头合成：归一化 → TTS（可选）→ 配音+字幕烧录，返回合成片段路径"""
    norm_path = str(merged_dir / f"norm_{sid}.mp4")
    normalize_segment(video_path, norm_path, silent_rate)
    total_dur = get_video_duration(norm_path)
    text = (script or "").strip()
    if not text:
        return norm_path

    audio_path, srt_path = generate_tts(
        text, str(audio_dir / f"{sid}_voice"), voice=voice, rate=rate,
        fallback_duration=total_dur,
    )
    _clean_break_entries(srt_path)
    tts_dur = total_dur
    if audio_path and Path(audio_path).exists() and Path(audio_path).stat().st_size > 1000:
        tts_dur = get_video_duration(audio_path)
    video_pad = max(0.0, tts_dur - total_dur)
    audio_pad = max(0.0, total_dur - tts_dur)
    out_path = str(merged_dir / f"composed_{sid}.mp4")
    return compose_final(
        norm_path, audio_path, srt_path, out_path,
        video_pad=video_pad, audio_pad=audio_pad,
        ffmpeg_crf=crf, ffmpeg_pix_fmt=pix_fmt,
        font_family=font_family, font_size=font_size, **sub_style,
    )


def run(shot_results: list[dict], config: dict) -> str:
    """
    完整合并流程：先拼接所有镜头 → 全局 TTS + 字幕 → 最终一次合成

    Args:
        shot_results: 每个镜头的结果 dict，需包含:
            - shot_id, status, video_path, dialogue, screen_text
        config: 全局配置

    Returns:
        最终视频路径
    """
    log.info(Msg.MERGE_START)

    output_cfg = config.get("output", {})
    tts_cfg = config.get("tts", {})
    ffmpeg_cfg = config.get("ffmpeg", {})
    merge_cfg = config.get("merge", {})

    audio_dir = Path(output_cfg.get("audio_dir", "output/audio"))
    subs_dir = Path(output_cfg.get("subs_dir", "output/subs"))
    merged_dir = Path(output_cfg.get("merged_dir", "output/merged"))
    final_dir = Path(output_cfg.get("final_dir", "output/final"))
    for d in (audio_dir, subs_dir, merged_dir, final_dir):
        d.mkdir(parents=True, exist_ok=True)

    voice = tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural")
    rate = tts_cfg.get("rate", "+0%")
    crf = ffmpeg_cfg.get("crf", 13)
    pix_fmt = ffmpeg_cfg.get("pix_fmt", "yuv420p")
    font_family = ffmpeg_cfg.get("font_family", "Microsoft YaHei")
    font_size = int(ffmpeg_cfg.get("font_size", 48))
    break_ms = int(merge_cfg.get("break_between_shots_ms", 600))
    silent_rate = int(merge_cfg.get("silent_sample_rate", 44100))
    tts_mode = merge_cfg.get("tts_mode", "whole")
    sub_cfg = ffmpeg_cfg.get("subtitle", {}) or {}
    sub_style = {
        "font_color": sub_cfg.get("font_color", "FFFFFF"),
        "outline_color": sub_cfg.get("outline_color", "000000"),
        "outline_width": int(sub_cfg.get("outline_width", 2)),
        "alignment": int(sub_cfg.get("alignment", 2)),
        "margin_v": int(sub_cfg.get("margin_v", 40)),
        "shadow": int(sub_cfg.get("shadow", 0)),
    }

    # 收集有效镜头
    valid = []
    for r in shot_results:
        if r.get("status") != "done":
            continue
        video_path = r.get("video_path", "")
        if video_path and Path(video_path).exists():
            valid.append(r)
        else:
            log.warning(Msg.MERGE_SKIP.format(id=r.get("shot_id", "?")))
    if not valid:
        raise RuntimeError("没有可合并的视频")

    log.info(Msg.MERGE_TOTAL_COUNT.format(count=len(valid)))
    batch_id = config.get("_batch_id", "")
    output_filename = f"{batch_id}.mp4" if batch_id else "final_video.mp4"
    output_path = str(final_dir / output_filename)

    # ── Per-shot 模式：每个镜头独立 TTS + 字幕烧录，再拼接 ──
    if tts_mode == "per_shot":
        log.info("per-shot 模式：逐镜头配音+字幕")
        composed = []
        for r in valid:
            sid = str(r.get("shot_id", "?"))
            script = (r.get("dialogue") or "").strip() or (r.get("screen_text") or "").strip()
            seg = _compose_shot_segment(
                r.get("video_path", ""), sid, script,
                audio_dir, merged_dir, voice, rate, silent_rate,
                crf, pix_fmt, font_family, font_size, sub_style,
            )
            composed.append(seg)
        return concat_videos(composed, output_path, crf, pix_fmt)

    # ── Stage 1: 归一化 + 拼接 ──
    normalized = []
    for r in valid:
        sid = r.get("shot_id", "?")
        video_path = r.get("video_path", "")
        dur = get_video_duration(video_path)
        log.info(Msg.MERGE_DURATION.format(file=Path(video_path).name, dur=f"{dur:.1f}"))
        norm_path = str(merged_dir / f"norm_{sid}.mp4")
        normalized.append(normalize_segment(video_path, norm_path, silent_rate))

    merged_name = f"{batch_id}_merged.mp4" if batch_id else "merged.mp4"
    merged_path = concat_videos(normalized, str(merged_dir / merged_name), crf, pix_fmt)
    total_dur = get_video_duration(merged_path)
    log.info(Msg.MERGE_TOTAL.format(dur=f"{total_dur:.1f}"))

    # none 模式：不配音不加字幕，纯拼接
    if tts_mode == "none":
        log.info("tts_mode=none：跳过配音与字幕")
        return merged_path

    # ── Stage 2+3: 全局 TTS + 全局字幕（时间轴从 0 到总时长）──
    script = build_global_script(valid, break_ms)
    audio_path = srt_path = ""
    tts_dur = 0.0
    if script:
        log.info(Msg.MERGE_TTS_GLOBAL.format(chars=len(script)))
        voice_name = f"{batch_id}_voice" if batch_id else "voice"
        audio_path, srt_path = generate_tts(
            script, str(audio_dir / voice_name),
            voice=voice, rate=rate, fallback_duration=total_dur,
        )
        _clean_break_entries(srt_path)
        if audio_path and Path(audio_path).exists() and Path(audio_path).stat().st_size > 1000:
            tts_dur = get_video_duration(audio_path)
        else:
            tts_dur = total_dur
    else:
        log.info(Msg.MERGE_NO_DIALOGUE)

    # 时长兜底
    video_pad = max(0.0, tts_dur - total_dur)
    audio_pad = max(0.0, total_dur - tts_dur)
    if video_pad > 0:
        log.info(Msg.MERGE_PAD_VIDEO.format(dur=f"{video_pad:.1f}"))
    if audio_pad > 0:
        log.info(Msg.MERGE_PAD_AUDIO.format(dur=f"{audio_pad:.1f}"))

    # ── Stage 4: 最终一次合成（替换原声 + 烧录全局字幕）──
    return compose_final(
        merged_path, audio_path, srt_path, output_path,
        video_pad=video_pad, audio_pad=audio_pad,
        ffmpeg_crf=crf, ffmpeg_pix_fmt=pix_fmt,
        font_family=font_family, font_size=font_size, **sub_style,
    )