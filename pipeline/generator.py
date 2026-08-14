"""
普通 async 编排：单镜头生成 + 批量并发
替代原 LangGraph 8 节点图（analyze→select→optimize→validate→submit→wait→review）
v1 起移除 langgraph 依赖
"""
import asyncio
from pathlib import Path

from .registry import TemplateRegistry
from .comfyui import ComfyUIClient
from .log import get_logger
from .messages import Msg

log = get_logger("generator")

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

    # 图片不存在时 fallback 到 T2V
    if asset_type in ("image", "local") and asset_path:
        if not Path(asset_path).exists():
            log.warning(f"镜号 {sid}: 素材 {asset_path} 不存在，fallback 到 T2V")
            asset_type = "ai_generated"

    wid = registry.find_best(
        asset_type=asset_type,
        scene_desc=state.get("scene_desc", ""),
    )
    wdata = registry.get(wid)
    wtype = wdata.get("workflow_type", "comfyui") if wdata else "comfyui"
    log.info(Msg.LG_SELECT.format(wid=wid, wtype=wtype))
    return {**state, "workflow_id": wid, "workflow_type": wtype, "workflow_data": wdata or {},
            "asset_type": asset_type}


def _optimize(state: dict) -> dict:
    config = state.get("config", {})
    elements = state.get("key_elements", [])
    duration = state.get("duration", config.get("agent", {}).get("default_duration", 5))
    desc_en = ", ".join(elements[:6]) if elements else "professional technology scene"
    prompt = f"{desc_en}. {CAMERA}, {STYLE}."
    if duration > config.get("agent", {}).get("prompt_long_duration", 8):
        prompt = prompt.rstrip(".") + ", slow paced, extended sequence."
    elif duration < config.get("agent", {}).get("prompt_short_duration", 4):
        prompt = prompt.rstrip(".") + ", quick dynamic, energetic pace."
    log.info(Msg.LG_OPTIMIZE.format(prompt=prompt[:55]))
    return {**state, "optimized_prompt": prompt}


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
    config = state.get("config", {})
    duration = state.get("duration", config.get("agent", {}).get("default_duration", 5))
    for nid, info in wf.items():
        if not info:
            continue
        ct = info.get("class_type", "")
        if "JimengSeedance" in ct:
            info["inputs"]["prompt"] = optimized
            info["inputs"]["duration"] = duration
            info["inputs"]["seed"] = config.get("agent", {}).get("default_seed", 42)
            batch_id = config.get("_batch_id", "")
            prefix = f"{batch_id}_shot_{sid}" if batch_id else f"shot_{sid}"
            info["inputs"]["filename_prefix"] = prefix
        if ct == "SaveVideo":
            batch_id = config.get("_batch_id", "")
            prefix = f"{batch_id}_shot_{sid}" if batch_id else f"shot_{sid}"
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
    try:
        pid = await client.submit(wf)
        log.info(Msg.LG_SUBMIT_OK.format(pid=pid))
        return {**state, "prompt_id": pid, "status": "submitted"}
    except Exception as e:
        log.error(f"提交失败: {e}")
        return {**state, "status": "failed", "error_message": f"提交失败: {e}"}


async def _wait(state: dict) -> dict:
    sid = state.get("id", "?")
    ctx = state.get("_ctx") or {}
    client = ctx.get("client")
    pid = state.get("prompt_id", "")
    if not client or not pid:
        return {**state, "status": "failed", "error_message": "无 client 或 prompt_id"}
    try:
        history = await client.wait_for_completion(pid)
        config = state.get("config", {})
        output_dir = config.get("output", {}).get("shots_dir", "output/shots")
        video_path = await client.download_output(history, output_dir, sid)
        log.info(Msg.LG_WAIT_OK.format(path=video_path))
        return {**state, "video_path": video_path, "status": "done"}
    except Exception as e:
        log.error(Msg.LG_WAIT_FAIL.format(err=e))
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
    ctx = ctx or {"registry": registry, "client": client}
    state = {
        "id": shot.get("id", ""),
        "shot_id": shot.get("id", ""),
        "scene_desc": shot.get("scene_desc", ""),
        "dialogue": shot.get("dialogue", ""),
        "screen_text": shot.get("screen_text", ""),
        "asset_type": shot.get("asset_type", ""),
        "asset_path": shot.get("asset_path", ""),
        "duration": shot.get("duration", config.get("input", {}).get("default_duration", 5)),
        "config": config,
        "_ctx": ctx,
        "retry_count": 0,
        "max_retries": config.get("agent", {}).get("max_retries", 2),
    }
    _ensure_agent_config(state)
    log.info(f"── 镜号 {state['id']} ──")

    state = _analyze(state)

    # validate 失败重试循环（等价 route_after_validate：回退到 select，最多 max_retries 次）
    for _ in range(state["max_retries"] + 1):
        state = _select(state)
        state = _optimize(state)
        state = _validate(state)
        if state.get("status") == "validated":
            break

    if state.get("status") != "validated":
        return _strip(state)

    state = await _submit(state)
    if state.get("status") == "pending_ffmpeg":
        return _strip(state)

    state = await _wait(state)
    state = _review(state)
    return _strip(state)


async def run_batch(shots: list[dict], config: dict, registry: TemplateRegistry,
                    client: ComfyUIClient = None, max_concurrency: int = 2) -> list[dict]:
    """批量并发生成镜头，asyncio.Semaphore 限制并发数"""
    ctx = {"registry": registry, "client": client}
    log.info(Msg.LG_BATCH_START.format(count=len(shots)))
    sem = asyncio.Semaphore(max_concurrency)

    async def one(shot: dict) -> dict:
        async with sem:
            try:
                return await generate_shot(shot, config, ctx=ctx)
            except Exception as e:
                log.error(f"镜号 {shot.get('id', '?')} 生成异常: {e}")
                base = {k: v for k, v in shot.items() if k not in ("_ctx", "config")}
                return {**base, "status": "failed", "error_message": str(e)}

    results = await asyncio.gather(*[one(s) for s in shots])
    done = sum(1 for r in results if r.get("status") == "done")
    failed = sum(1 for r in results if r.get("status") == "failed")
    ffmpeg = sum(1 for r in results if r.get("status") == "pending_ffmpeg")
    log.info(Msg.LG_BATCH_DONE.format(done=done, failed=failed, ffmpeg=ffmpeg))
    return results
