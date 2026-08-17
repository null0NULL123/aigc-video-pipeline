"""
普通 async 编排：单镜头生成 + 批量并发
替代原 LangGraph 8 节点图（analyze→select→optimize→validate→submit→wait→review）
v1 起移除 langgraph 依赖
"""
import asyncio
from pathlib import Path
import sys
import time

from .registry import TemplateRegistry
from .providers.comfyui import ComfyUIClient
from .providers.volcano import VolcanoClient, VolcanoSeedream, VolcanoSeedance
from .log import get_logger
from .messages import Msg
from . import llm as llm_client

log = get_logger("generator")


# 进程内 batch_id；由 cli.py / pipeline.py 设进去；用于 marker 输出
_CURRENT_BATCH_ID: list[str | None] = [None]


def set_current_batch_id(batch_id: str | None) -> None:
    """设置当前 batch_id（由 cli.py / pipeline.py 在 spawn subprocess 前调用）"""
    _CURRENT_BATCH_ID[0] = batch_id


def emit_event(event: str, shot_key: str = "", **fields) -> None:
    """
    向 stdout 输出 marker 行，供 web 后端解析后更新 tracker。

    格式：@@PIPE_EVENT@@ {json}
    - event: shot_start / shot_progress / shot_done / shot_failed / batch_done
    - shot_key: 镜头 key（temp_id 字符串 / table_id+shot_id）
    - 其他字段透传给 tracker.update_shot(**fields)
    """
    import json as _json
    batch_id = _CURRENT_BATCH_ID[0] or ""
    payload = {
        "event": event,
        "batch_id": batch_id,
        "shot_key": str(shot_key) if shot_key else "",
        "ts": time.time(),
        **fields,
    }
    line = f"{Msg.PIPE_EVENT_PREFIX} {_json.dumps(payload, ensure_ascii=False)}"
    print(line, flush=True)

# 运行时从 config 加载
CAMERA = ""
STYLE = ""


def _ensure_agent_config(state: dict):
    """从 config 加载 agent 配置到全局变量"""
    global CAMERA, STYLE
    agent_cfg = state.get("config", {}).get("agent", {})
    if not CAMERA:
        CAMERA = agent_cfg.get("camera", "smooth slow camera push-in")
    if not STYLE:
        STYLE = agent_cfg.get("style", "cinematic lighting, professional corporate atmosphere, modern clean design")


def _analyze(state: dict) -> dict:
    sid = state.get("id", "?")
    desc = state.get("scene_desc", "")
    log.info(f"镜号 {sid}: {desc[:40]}")
    return state


def _select(state: dict) -> dict:
    sid = state.get("id", "?")
    registry = state.get("_ctx", {}).get("registry")
    if not registry:
        log.info(Msg.LG_SELECT_FALLBACK)
        return {**state, "workflow_id": "seedance_i2v", "workflow_type": "comfyui", "workflow_data": {}}

    asset_type = state.get("asset_type", "")
    asset_path = state.get("asset_path", "")
    assets = state.get("assets") or []
    first_frame = state.get("first_frame", "")

    # 新素材模型（assets[] 多路 + 首帧/尾帧）推导主类型
    if assets or first_frame:
        img_assets = [a for a in assets if a.get("type") == "image" and a.get("path")]
        vid_assets = [a for a in assets if a.get("type") == "video" and a.get("path")]
        if img_assets or first_frame:
            asset_type = "image"
            asset_path = first_frame or img_assets[0]["path"]
        elif vid_assets:
            asset_type = "ai_generated"  # 视频仅作参考
        else:
            asset_type = "ai_generated"

    # 图片不存在时 fallback 到 T2V
    if asset_type in ("image", "local") and asset_path:
        if not Path(asset_path).exists():
            log.warning(f"镜号 {sid}: 素材 {asset_path} 不存在，fallback 到 T2V")
            asset_type = "ai_generated"

    # 镜头显式指定工作流：存在则直接采用，否则回退自动匹配
    wid = state.get("workflow_id", "")
    if wid:
        try:
            wdata = registry.get(wid)
            log.info(f"镜号 {sid}: 使用显式工作流 {wid}")
            return {**state, "workflow_id": wid,
                    "workflow_type": wdata.get("workflow_type", "comfyui"),
                    "workflow_data": wdata, "asset_type": asset_type}
        except KeyError:
            log.warning(f"镜号 {sid}: 显式工作流 {wid} 不存在，回退自动匹配")
            wid = ""

    wid = registry.find_best(
        asset_type=asset_type,
        scene_desc=state.get("scene_desc", ""),
    )
    wdata = registry.get(wid)
    wtype = wdata.get("workflow_type", "comfyui") if wdata else "comfyui"
    log.info(Msg.LG_SELECT.format(wid=wid, wtype=wtype))
    return {**state, "workflow_id": wid, "workflow_type": wtype, "workflow_data": wdata or {},
            "asset_type": asset_type}


def _workflow_class_types(wdata: dict) -> set:
    """收集工作流中的所有 class_type（用于链式检测）"""
    wf = wdata.get("workflow") if isinstance(wdata, dict) else None
    if not isinstance(wf, dict):
        return set()
    return {n.get("class_type", "") for n in wf.values() if isinstance(n, dict)}


def _is_chain_workflow(wdata: dict) -> bool:
    """链式：同一工作流内同时含 JimengSeedream(图) + JimengSeedance(视频)"""
    cts = _workflow_class_types(wdata)
    return (any("JimengSeedream" in c for c in cts) and
            any("JimengSeedance" in c for c in cts))


# 火山方舟直连可处理的节点：Jimeng 系列 + 文件 I/O glue 节点
_VOLCANO_NODE_PREFIXES = ("JimengSeedream", "JimengSeedance", "JimengAPIClient")
_VOLCANO_GLUE_NODES = ("LoadImage", "LoadVideo", "SaveImage", "SaveVideo")


def _is_volcano_eligible(wf: dict) -> bool:
    """工作流是否可走火山方舟直连（只含 Jimeng + 文件 I/O glue 节点）"""
    if not wf or not isinstance(wf, dict):
        return False
    for info in wf.values():
        if not isinstance(info, dict):
            continue
        ct = info.get("class_type", "")
        if any(ct.startswith(p) for p in _VOLCANO_NODE_PREFIXES):
            continue
        if ct in _VOLCANO_GLUE_NODES:
            continue
        return False  # 未知节点 → 只能走 ComfyUI
    return True


def _has_volcano_key(config: dict) -> bool:
    """config.api.jimeng.api_key 已配置且不是占位符"""
    key = (config.get("api", {}).get("jimeng", {}).get("api_key", "") or "").strip()
    return bool(key) and not key.startswith("your-")


async def _optimize_prompt(state: dict) -> dict:
    """LLM 把画面内容翻译成英文 prompt；失败/未配置时回退静态拼接"""
    config = state.get("config", {})
    elements = state.get("key_elements", [])
    duration = state.get("duration", config.get("agent", {}).get("default_duration", 5))

    desc = state.get("scene_desc", "")
    dialogue = state.get("dialogue", "")
    prompt = None
    if llm_client.is_enabled(config):
        prompt = await llm_client.translate_prompt(config, desc, dialogue, duration)
        if prompt:
            log.info(f"镜号 {state.get('id', '?')}: LLM prompt 生成完成")

    if not prompt:
        agent_cfg = config.get("agent", {}) or {}
        camera = agent_cfg.get("camera", CAMERA) or "smooth slow camera push-in"
        style = agent_cfg.get("style", STYLE) or "cinematic lighting, professional corporate atmosphere, modern clean design"
        desc_en = ", ".join(elements[:6]) if elements else "professional technology scene"
        prompt = f"{desc_en}. {camera}, {style}."

    if duration > config.get("agent", {}).get("prompt_long_duration", 8):
        prompt = prompt.rstrip(".") + ", slow paced, extended sequence."
    elif duration < config.get("agent", {}).get("prompt_short_duration", 4):
        prompt = prompt.rstrip(".") + ", quick dynamic, energetic pace."
    log.info(Msg.LG_OPTIMIZE.format(prompt=prompt[:55]))

    result = {**state, "optimized_prompt": prompt}

    # 链式工作流（Seedream 出图 → Seedance 动画化）额外生成动作 prompt
    if _is_chain_workflow(state.get("workflow_data") or {}):
        agent_cfg = config.get("agent", {}) or {}
        camera = agent_cfg.get("camera", CAMERA) or "smooth slow camera push-in"
        motion = f"{camera}, subtle cinematic motion, natural gentle movement."
        if duration > config.get("agent", {}).get("prompt_long_duration", 8):
            motion = motion.rstrip(".") + ", slow paced, extended sequence."
        elif duration < config.get("agent", {}).get("prompt_short_duration", 4):
            motion = motion.rstrip(".") + ", quick dynamic, energetic pace."
        result["motion_prompt"] = motion
        log.info(f"镜号 {state.get('id', '?')}: 链式工作流，生成 motion_prompt: {motion[:55]}")
    return result


async def _polish(state: dict) -> dict:
    """LLM 润色台词 + 补全屏幕字幕（失败则保持原值）"""
    config = state.get("config", {})
    if not llm_client.is_enabled(config):
        return state

    dialogue = state.get("dialogue", "") or ""
    screen_text = state.get("screen_text", "") or ""
    new_dialogue, new_screen_text = dialogue, screen_text

    if dialogue.strip():
        polished = await llm_client.polish_dialogue(config, dialogue)
        if polished:
            new_dialogue = polished
    if not screen_text.strip() and (dialogue.strip() or state.get("scene_desc", "")):
        suggested = await llm_client.suggest_screen_text(config, state.get("scene_desc", ""), dialogue)
        if suggested:
            new_screen_text = suggested

    if new_dialogue != dialogue or new_screen_text != screen_text:
        log.info(f"镜号 {state.get('id', '?')}: 台词/字幕已润色")
    return {**state, "dialogue": new_dialogue, "screen_text": new_screen_text}


def _validate(state: dict) -> dict:
    sid = state.get("id", "?")
    config = state.get("config", {})
    issues = []
    dur = state.get("duration", 0)
    dur_range = config.get("agent", {}).get("duration_range", [2, 20])
    if dur < dur_range[0] or dur > dur_range[1]:
        issues.append(f"时长 {dur}s 超出范围 {dur_range}")
    prompt = state.get("optimized_prompt", "")
    if len(prompt) < config.get("agent", {}).get("prompt_min_length", 10):
        issues.append("Prompt 过短")
    if not state.get("workflow_data"):
        issues.append("未选择模板")
    if issues:
        msg = "; ".join(issues)
        log.warning(Msg.LG_VALIDATE_FAIL.format(msg=msg))
        return {**state, "status": "retry", "error_message": msg,
                "retry_count": state.get("retry_count", 0) + 1}
    log.info(Msg.LG_VALIDATE_OK)
    return {**state, "status": "validated", "error_message": ""}


async def _submit(state: dict) -> dict:
    sid = state.get("id", "?")
    ctx = state.get("_ctx") or {}
    config = state.get("config", {})

    # ── 火山方舟直连分发：Jimeng 工作流 + api_key 已配 + client 已建 ──
    wf_data = state.get("workflow_data") or {}
    wf = wf_data.get("workflow") if isinstance(wf_data, dict) else None
    if (_is_volcano_eligible(wf) and _has_volcano_key(config)
            and ctx.get("volcano_client")):
        return await _submit_volcano(state)

    # ── 原 ComfyUI 路径（兜底，支持任意节点） ──
    client = ctx.get("client")
    wtype = state.get("workflow_type", "")
    if wtype != "comfyui" or not client:
        log.warning(Msg.LG_SUBMIT_SKIP)
        return {**state, "status": "pending_ffmpeg"}
    wf_data = state.get("workflow_data") or {}
    wf = wf_data.get("workflow") if isinstance(wf_data, dict) else None
    if not wf:
        return {**state, "status": "failed", "error_message": "无 workflow 数据"}
    import json as _json
    wf = _json.loads(_json.dumps(wf))
    optimized = state.get("optimized_prompt", "")
    motion = state.get("motion_prompt", "")
    is_chain = _is_chain_workflow(wf_data) or bool(motion)
    config = state.get("config", {})
    duration = state.get("duration", config.get("agent", {}).get("default_duration", 5))
    batch_id = config.get("_batch_id", "")
    prefix = f"{batch_id}_shot_{sid}" if batch_id else f"shot_{sid}"
    for nid, info in wf.items():
        if not info:
            continue
        ct = info.get("class_type", "")
        if "JimengSeedream" in ct:
            info["inputs"]["prompt"] = optimized
            info["inputs"]["seed"] = config.get("agent", {}).get("default_seed", 42)
        elif "JimengSeedance" in ct:
            info["inputs"]["prompt"] = motion if is_chain else optimized
            info["inputs"]["duration"] = duration
            info["inputs"]["seed"] = config.get("agent", {}).get("default_seed", 42)
            info["inputs"]["filename_prefix"] = prefix
        if ct in ("SaveVideo", "SaveImage"):
            info["inputs"]["filename_prefix"] = prefix
    asset_path = state.get("asset_path", "")
    asset_type = state.get("asset_type", "")
    if asset_type in ("image", "local") and asset_path:
        if Path(asset_path).exists():
            try:
                server_fn = await client.upload_image(asset_path)
                for nid, info in wf.items():
                    if info and info.get("class_type") == "LoadImage":
                        info["inputs"]["image"] = server_fn
                        break
            except Exception as e:
                return {**state, "status": "failed", "error_message": f"上传失败: {e}"}

    # ── 多素材 / 首帧尾帧注入 ─────────────────────────────
    assets = state.get("assets") or []
    first_frame = state.get("first_frame", "")
    last_frame = state.get("last_frame", "")
    img_assets = [a.get("path") for a in assets
                  if a.get("type") == "image" and a.get("path")]
    vid_assets = [a.get("path") for a in assets
                  if a.get("type") == "video" and a.get("path")]

    first_img = first_frame or (img_assets[0] if img_assets else "")
    last_img = last_frame or ""
    extra_imgs = [p for p in img_assets if p != first_img]

    async def _upload(path: str) -> str:
        if not Path(path).exists():
            raise FileNotFoundError(f"素材不存在: {path}")
        return await client.upload_image(path)

    def _seedance_nodes():
        return [(nid, info) for nid, info in wf.items()
                if info and "JimengSeedance" in info.get("class_type", "")]

    # 首帧：注入第一个 LoadImage；无 LoadImage 节点则动态创建并接 first_frame_image
    if first_img and first_img != asset_path:
        try:
            server_fn = await _upload(first_img)
            load_nodes = [nid for nid, info in wf.items()
                          if info and info.get("class_type") == "LoadImage"]
            if load_nodes:
                wf[load_nodes[0]]["inputs"]["image"] = server_fn
            else:
                nid = "__first_frame"
                wf[nid] = {"class_type": "LoadImage", "inputs": {"image": server_fn}}
                for snid, sinfo in _seedance_nodes():
                    sinfo["inputs"]["first_frame_image"] = [nid, 0]
        except Exception as e:
            log.warning(f"镜号 {sid}: 首帧注入失败，继续: {e}")

    # 尾帧：动态 LoadImage 接 last_frame_image（失败降级）
    if last_img:
        try:
            server_fn = await _upload(last_img)
            nid = "__last_frame"
            wf[nid] = {"class_type": "LoadImage", "inputs": {"image": server_fn}}
            for snid, sinfo in _seedance_nodes():
                sinfo["inputs"]["last_frame_image"] = [nid, 0]
        except Exception as e:
            log.warning(f"镜号 {sid}: 尾帧注入失败，已降级: {e}")

    # 多图参考 ref_images（best-effort；autogrow 槽位名为 ref_image_1..9）
    if extra_imgs:
        try:
            refs = {}
            for i, p in enumerate(extra_imgs[:9]):
                s = await _upload(p)
                nid = f"__ref_img{i}"
                wf[nid] = {"class_type": "LoadImage", "inputs": {"image": s}}
                refs[f"ref_image_{i + 1}"] = [nid, 0]
            if refs:
                for snid, sinfo in _seedance_nodes():
                    sinfo["inputs"]["ref_images"] = refs
        except Exception as e:
            log.warning(f"镜号 {sid}: 参考图注入失败，已降级: {e}")

    # 视频参考 ref_videos（best-effort；LoadVideo 输入键 file，autogrow 槽位 ref_video_1..3）
    if vid_assets:
        try:
            refs_v = {}
            for i, p in enumerate(vid_assets[:3]):
                if not Path(p).exists():
                    continue
                s = await client.upload_video(p)
                nid = f"__ref_vid{i}"
                wf[nid] = {"class_type": "LoadVideo", "inputs": {"file": s}}
                refs_v[f"ref_video_{i + 1}"] = [nid, 0]
            if refs_v:
                for snid, sinfo in _seedance_nodes():
                    sinfo["inputs"]["ref_videos"] = refs_v
        except Exception as e:
            log.warning(f"镜号 {sid}: 参考视频注入失败，已降级: {e}")

    try:
        pid = await client.submit(wf)
        log.info(Msg.LG_SUBMIT_OK.format(pid=pid))
        return {**state, "prompt_id": pid, "status": "submitted"}
    except Exception as e:
        log.error(f"提交失败: {e}")
        return {**state, "status": "failed", "error_message": f"提交失败: {e}"}


async def _wait(state: dict) -> dict:
    # 火山方舟路径：状态由 _submit_volcano 设置
    if state.get("_provider") == "volcano":
        return await _wait_volcano(state)

    sid = state.get("id", "?")
    ctx = state.get("_ctx") or {}
    client = ctx.get("client")
    pid = state.get("prompt_id", "")
    if not client or not pid:
        return {**state, "status": "failed", "error_message": "无 client 或 prompt_id"}
    try:
        history = await client.wait_for_completion(pid)
        config = state.get("config", {})
        output_dir = config.get("output", {}).get("shots_dir", "output/shots/{project}")
        video_path = await client.download_output(history, output_dir, sid)
        log.info(Msg.LG_WAIT_OK.format(path=video_path))
        return {**state, "video_path": video_path, "status": "done"}
    except Exception as e:
        log.error(Msg.LG_WAIT_FAIL.format(err=e))
        return {**state, "status": "failed", "error_message": str(e)}


# ──────────────── 火山方舟直连路径（无需 ComfyUI） ────────────────

def _volcano_seedream_params(wf: dict, config: dict, sid: str) -> tuple[str, str, dict]:
    """从工作流 + config 提取 Seedream 参数"""
    sr_cfg = config.get("seedream", {})
    default_seed = int(config.get("agent", {}).get("default_seed", 0) or 0)
    node = next((n for n in wf.values()
                 if isinstance(n, dict) and "JimengSeedream" in n.get("class_type", "")),
                None)
    sin = (node or {}).get("inputs", {})
    return (
        sin.get("model_version", sr_cfg.get("model_version", "doubao-seedream-4.0")),
        sin.get("size", sr_cfg.get("size", "2K")),
        {
            "seed": int(sin.get("seed", default_seed) or 0),
            "watermark": bool(sin.get("watermark", sr_cfg.get("watermark", False))),
        },
    )


def _volcano_seedance_params(wf: dict, config: dict, duration: int) -> tuple[str, str, str, dict]:
    """从工作流 + config 提取 Seedance 参数"""
    sd_cfg = config.get("seedance", {})
    default_seed = int(config.get("agent", {}).get("default_seed", 0) or 0)
    node = next((n for n in wf.values()
                 if isinstance(n, dict) and "JimengSeedance" in n.get("class_type", "")),
                None)
    sin = (node or {}).get("inputs", {})
    return (
        sin.get("model_version", sd_cfg.get("model_version", "doubao-seedance-2-0-fast")),
        sin.get("resolution", sd_cfg.get("resolution", "720p")),
        sin.get("aspect_ratio", sd_cfg.get("aspect_ratio", "16:9")),
        {
            "duration": int(sin.get("duration", duration) or duration),
            "generate_audio": bool(sin.get("generate_audio", sd_cfg.get("generate_audio", True))),
            "seed": int(sin.get("seed", default_seed) or 0),
        },
    )


async def _download_image_result(client: VolcanoClient, result: dict,
                                  dest: Path) -> Path:
    """从 Seedream 响应拿到图片 URL，下载到本地"""
    data = result.get("data") or []
    if not data:
        raise RuntimeError(f"Seedream 返回空 data: {result}")
    item = data[0]
    url = item.get("url") or item.get("b64_json")
    if not url:
        raise RuntimeError(f"Seedream 返回无 url/b64_json: {item}")
    if url.startswith("http"):
        return await client.download(url, dest)
    # b64_json
    import base64
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(url))
    log.info(Msg.GEN_DOWNLOAD.format(path=str(dest)))
    return dest


async def _submit_volcano(state: dict) -> dict:
    """火山方舟直连提交。处理单 Seedream / 单 Seedance / 链式 Seedream→Seedance

    返回 state：
    - 单 Seedream：status=done, video_path=本地 PNG 路径
    - 单 Seedance / 链式：status=submitted, task_id=Seedance 任务 ID, _provider="volcano"
    """
    sid = state.get("id", "?")
    config = state.get("config", {})
    ctx = state.get("_ctx") or {}
    vc: VolcanoClient = ctx.get("volcano_client")
    if not vc:
        return {**state, "status": "failed", "error_message": "无 VolcanoClient"}

    wf_data = state.get("workflow_data") or {}
    wf = wf_data.get("workflow") or {}
    seedream_nodes = [n for n in wf.values()
                      if isinstance(n, dict) and "JimengSeedream" in n.get("class_type", "")]
    seedance_nodes = [n for n in wf.values()
                      if isinstance(n, dict) and "JimengSeedance" in n.get("class_type", "")]

    optimized = state.get("optimized_prompt", "")
    motion = state.get("motion_prompt", "")
    is_chain = _is_chain_workflow(wf_data)
    sd_cfg = config.get("seedance", {})
    duration = int(state.get("duration", sd_cfg.get("default_duration", 5)))
    batch_id = config.get("_batch_id", "")
    prefix = f"{batch_id}_shot_{sid}" if batch_id else f"shot_{sid}"
    output_dir = Path(config.get("output", {}).get("shots_dir", "output/shots/{project}"))

    # 素材收集
    assets = state.get("assets") or []
    first_frame = state.get("first_frame", "")
    last_frame = state.get("last_frame", "")
    img_assets = [a.get("path") for a in assets
                  if a.get("type") == "image" and a.get("path")]
    vid_assets = [a.get("path") for a in assets
                  if a.get("type") == "video" and a.get("path")]
    first_img = first_frame or (img_assets[0] if img_assets else "")
    extra_imgs = [p for p in img_assets if p and p != first_img]

    seedream_api = VolcanoSeedream(vc)
    seedance_api = VolcanoSeedance(vc)

    try:
        first_frame_url = ""
        # 先跑 Seedream（如有）
        if seedream_nodes:
            model, size, extra = _volcano_seedream_params(wf, config, sid)
            images = [u for u in ([first_img] + extra_imgs) if u] or None
            sr_result = await seedream_api.generate(
                prompt=optimized,
                model=model, size=size,
                seed=extra["seed"], watermark=extra["watermark"],
                image=images,
            )
            first_frame_url = (sr_result.get("data") or [{}])[0].get("url", "")
            log.info(f"镜号 {sid}: Seedream 完成，first_frame_url={'已获取' if first_frame_url else '空'}")

        # 单 Seedream + 无 Seedance → 同步出图，直接下载
        if seedream_nodes and not seedance_nodes:
            dest = output_dir / f"{prefix}.png"
            local = await _download_image_result(vc, sr_result, dest)
            log.info(Msg.LG_WAIT_OK.format(path=str(local)))
            return {**state, "video_path": str(local), "status": "done",
                    "_provider": "volcano"}

        # Seedance（链式 / 单 Seedance）
        if seedance_nodes:
            model, resolution, aspect_ratio, extra = _volcano_seedance_params(wf, config, duration)
            sd_prompt = motion if is_chain else optimized
            task_id = await seedance_api.submit(
                prompt=sd_prompt,
                model=model, resolution=resolution, aspect_ratio=aspect_ratio,
                duration=extra["duration"],
                generate_audio=extra["generate_audio"],
                seed=extra["seed"],
                first_frame=first_frame_url or first_frame or "",
                last_frame=last_frame or "",
                ref_images=[u for u in extra_imgs if u] or None,
                ref_videos=[u for u in vid_assets if u] or None,
                filename_prefix=prefix,
            )
            return {**state, "task_id": task_id, "status": "submitted",
                    "_provider": "volcano"}

        return {**state, "status": "failed", "error_message": "工作流无 Jimeng 节点"}
    except Exception as e:
        log.error(f"镜号 {sid} 火山提交失败: {e}")
        return {**state, "status": "failed", "error_message": f"火山提交失败: {e}"}


async def _wait_volcano(state: dict) -> dict:
    """轮询 Seedance 任务 + 下载视频"""
    sid = state.get("id", "?")
    config = state.get("config", {})
    ctx = state.get("_ctx") or {}
    vc: VolcanoClient = ctx.get("volcano_client")
    task_id = state.get("task_id", "")
    if not vc or not task_id:
        return {**state, "status": "failed", "error_message": "无 VolcanoClient 或 task_id"}

    batch_id = config.get("_batch_id", "")
    prefix = f"{batch_id}_shot_{sid}" if batch_id else f"shot_{sid}"
    output_dir = Path(config.get("output", {}).get("shots_dir", "output/shots/{project}"))

    try:
        seedance_api = VolcanoSeedance(vc)
        result = await seedance_api.wait_for_completion(task_id, max_wait=600)
        video_url = (result.get("content") or {}).get("video_url", "")
        if not video_url:
            return {**state, "status": "failed", "error_message": "Seedance 返回无 video_url"}
        local = output_dir / f"{prefix}.mp4"
        await vc.download(video_url, local)
        log.info(Msg.LG_WAIT_OK.format(path=str(local)))
        return {**state, "video_path": str(local), "status": "done"}
    except Exception as e:
        log.error(f"镜号 {sid} Seedance 等待失败: {e}")
        return {**state, "status": "failed", "error_message": str(e)}


def _review(state: dict) -> dict:
    status = state.get("status", "")
    if status != "done":
        log.warning(Msg.LG_REVIEW_SKIP.format(status=status))
        return {**state, "review_result": "skip"}
    log.info(Msg.LG_REVIEW_PASS)
    return {**state, "review_result": "pass"}


def _strip(state: dict) -> dict:
    """去除内部字段，返回结果 dict（与合并/Web 契约一致）"""
    return {k: v for k, v in state.items() if k not in ("_ctx", "config")}


async def generate_shot(shot: dict, config: dict, registry: TemplateRegistry = None,
                        client: ComfyUIClient = None, ctx: dict = None) -> dict:
    """单个镜头的完整生成链路（等价原 8 节点图：analyze→select→optimize→validate→submit→wait→review）"""
    if ctx is None:
        ctx = {"registry": registry, "client": client}
    state = {
        "id": shot.get("id", ""),
        "shot_id": shot.get("id", ""),
        "scene_desc": shot.get("scene_desc", ""),
        "dialogue": shot.get("dialogue", ""),
        "screen_text": shot.get("screen_text", ""),
        "asset_type": shot.get("asset_type", ""),
        "asset_path": shot.get("asset_path", ""),
        "assets": shot.get("assets") or [],
        "first_frame": shot.get("first_frame", ""),
        "last_frame": shot.get("last_frame", ""),
        "workflow_id": shot.get("workflow_id", ""),
        "duration": shot.get("duration", config.get("input", {}).get("default_duration", 5)),
        "config": config,
        "_ctx": ctx,
        "retry_count": 0,
        "max_retries": config.get("agent", {}).get("max_retries", 2),
    }
    _ensure_agent_config(state)
    log.info(f"── 镜号 {state['id']} ──")
    shot_key = str(state["id"])
    emit_event("shot_start", shot_key=shot_key,
               status="analyzing",
               scene_desc=state.get("scene_desc", ""),
               duration=state.get("duration", 5))

    state = _analyze(state)
    state = await _polish(state)
    emit_event("shot_progress", shot_key=shot_key, status="selected")

    # validate 失败重试循环（等价 route_after_validate：回退到 select，最多 max_retries 次）
    for _ in range(state["max_retries"] + 1):
        state = _select(state)
        state = await _optimize_prompt(state)
        state = _validate(state)
        if state.get("status") == "validated":
            break

    if state.get("status") != "validated":
        emit_event("shot_failed", shot_key=shot_key,
                   status="failed",
                   stage="validate",
                   error=state.get("error", "validate failed"))
        return _strip(state)

    emit_event("shot_progress", shot_key=shot_key, status="submitting")
    state = await _submit(state)
    if state.get("status") == "pending_ffmpeg":
        emit_event("shot_done", shot_key=shot_key,
                   status="pending_ffmpeg",
                   stage="ffmpeg-local")
        return _strip(state)

    emit_event("shot_progress", shot_key=shot_key, status="waiting")
    state = await _wait(state)
    state = _review(state)

    final_status = state.get("status", "done")
    if final_status == "done":
        emit_event("shot_done", shot_key=shot_key,
                   status="done",
                   stage="done",
                   video_path=state.get("video_path"))
    else:
        emit_event("shot_failed", shot_key=shot_key,
                   status="failed",
                   stage="review",
                   error=state.get("error", "review failed"))
    return _strip(state)


async def run_batch(shots: list[dict], config: dict, registry: TemplateRegistry,
                    client: ComfyUIClient = None, max_concurrency: int = 2,
                    volcano_client: VolcanoClient = None) -> list[dict]:
    """批量并发生成镜头，asyncio.Semaphore 限制并发数

    volcano_client: 可选，提供后 Jimeng 工作流走火山方舟直连（无需 ComfyUI）
    """
    ctx = {"registry": registry, "client": client, "volcano_client": volcano_client}
    log.info(Msg.LG_BATCH_START.format(count=len(shots)))
    sem = asyncio.Semaphore(max_concurrency)

    async def one(shot: dict) -> dict:
        async with sem:
            try:
                return await generate_shot(shot, config, ctx=ctx)
            except Exception as e:
                log.error(f"镜号 {shot.get('id', '?')} 生成异常: {e}")
                base = {k: v for k, v in shot.items() if k not in ("_ctx", "config")}
                emit_event("shot_failed", shot_key=str(shot.get("id", "")),
                           status="failed",
                           stage="exception",
                           error=str(e))
                return {**base, "status": "failed", "error_message": str(e)}

    results = await asyncio.gather(*[one(s) for s in shots])
    done = sum(1 for r in results if r.get("status") == "done")
    failed = sum(1 for r in results if r.get("status") == "failed")
    ffmpeg = sum(1 for r in results if r.get("status") == "pending_ffmpeg")
    log.info(Msg.LG_BATCH_DONE.format(done=done, failed=failed, ffmpeg=ffmpeg))
    emit_event("batch_done", shot_key="",
               exit_code=0,
               done=done, failed=failed, pending_ffmpeg=ffmpeg)
    return results
