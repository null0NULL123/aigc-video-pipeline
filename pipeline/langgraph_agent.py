"""
LangGraph Agent: 视频生产流水线
8 节点图: analyze → select → optimize → validate → submit → wait → review → END
"""
import asyncio
from pathlib import Path

from langgraph.graph import StateGraph, END

from .registry import TemplateRegistry
from .comfyui import ComfyUIClient
from .log import get_logger
from .llm import call_llm
from .messages import Msg

log = get_logger("langgraph")

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


def analyze_node(state: dict) -> dict:
    _ensure_agent_config(state)
    sid = state.get("id", "?")
    desc = state.get("scene_desc", "")
    log.info(f"镜号 {sid}: {desc[:40]}")
    return {**state}


def select_node(state: dict) -> dict:
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


def optimize_node(state: dict) -> dict:
    sid = state.get("id", "?")
    elements = state.get("key_elements", [])
    duration = state.get("duration", state.get("config", {}).get("agent", {}).get("default_duration", 5))
    desc_en = ", ".join(elements[:6]) if elements else "professional technology scene"
    prompt = f"{desc_en}. {CAMERA}, {STYLE}."
    if duration > state.get("config", {}).get("agent", {}).get("prompt_long_duration", 8):
        prompt = prompt.rstrip(".") + ", slow paced, extended sequence."
    elif duration < state.get("config", {}).get("agent", {}).get("prompt_short_duration", 4):
        prompt = prompt.rstrip(".") + ", quick dynamic, energetic pace."
    log.info(Msg.LG_OPTIMIZE.format(prompt=prompt[:55]))
    return {**state, "optimized_prompt": prompt}


def validate_node(state: dict) -> dict:
    sid = state.get("id", "?")
    issues = []
    dur = state.get("duration", 0)
    dur_range = state.get("config", {}).get("agent", {}).get("duration_range", [2, 20])
    if dur < dur_range[0] or dur > dur_range[1]:
        issues.append(f"时长 {dur}s 超出范围 {dur_range}")
    prompt = state.get("optimized_prompt", "")
    if len(prompt) < state.get("config", {}).get("agent", {}).get("prompt_min_length", 10):
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


async def submit_node(state: dict) -> dict:
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
    duration = state.get("duration", state.get("config", {}).get("agent", {}).get("default_duration", 5))
    for nid, info in wf.items():
        if not info:
            continue
        ct = info.get("class_type", "")
        if "JimengSeedance" in ct:
            info["inputs"]["prompt"] = optimized
            info["inputs"]["duration"] = duration
            info["inputs"]["seed"] = state.get("config", {}).get("agent", {}).get("default_seed", 42)
            batch_id = state.get("config", {}).get("_batch_id", "")
            prefix = f"{batch_id}_shot_{sid}" if batch_id else f"shot_{sid}"
            info["inputs"]["filename_prefix"] = prefix
        if ct == "SaveVideo":
            batch_id = state.get("config", {}).get("_batch_id", "")
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


async def wait_node(state: dict) -> dict:
    sid = state.get("id", "?")
    ctx = state.get("_ctx") or {}
    client = ctx.get("client")
    pid = state.get("prompt_id", "")
    if not client or not pid:
        return {**state, "status": "failed", "error_message": "无 client 或 prompt_id"}
    try:
        history = await client.wait_for_completion(pid)
        config = state.get("config", {})
        output_dir = config.get("output", {}).get("shots_dir", "output/shots")  # from config
        video_path = await client.download_output(history, output_dir, sid)
        log.info(Msg.LG_WAIT_OK.format(path=video_path))
        return {**state, "video_path": video_path, "status": "done"}
    except Exception as e:
        log.error(Msg.LG_WAIT_FAIL.format(err=e))
        return {**state, "status": "failed", "error_message": str(e)}


def review_node(state: dict) -> dict:
    sid = state.get("id", "?")
    status = state.get("status", "")
    if status != "done":
        log.warning(Msg.LG_REVIEW_SKIP.format(status=status))
        return {**state, "review_result": "skip"}
    log.info(Msg.LG_REVIEW_PASS)
    return {**state, "review_result": "pass"}


def route_after_validate(state: dict) -> str:
    if state.get("status") == "validated":
        return "submit"
    retry = state.get("retry_count", 0)
    max_r = state.get("max_retries", state.get("config", {}).get("agent", {}).get("max_retries", 2))
    if retry < max_r:
        return "select"
    return "fail"


def route_after_review(state: dict) -> str:
    result = state.get("review_result", "")
    if result == "pass":
        return "end"
    if result == "retry":
        retry = state.get("retry_count", 0)
        max_r = state.get("max_retries", state.get("config", {}).get("agent", {}).get("max_retries", 2))
        if retry < max_r:
            return "retry"
    return "fail"


def build_graph():
    workflow = StateGraph(dict)
    workflow.add_node("analyze", analyze_node)
    workflow.add_node("select", select_node)
    workflow.add_node("optimize", optimize_node)
    workflow.add_node("validate", validate_node)
    workflow.add_node("submit", submit_node)
    workflow.add_node("wait", wait_node)
    workflow.add_node("review", review_node)
    workflow.set_entry_point("analyze")
    workflow.add_edge("analyze", "select")
    workflow.add_edge("select", "optimize")
    workflow.add_edge("optimize", "validate")
    workflow.add_conditional_edges("validate", route_after_validate,
        {"submit": "submit", "select": "select", "fail": END})
    workflow.add_edge("submit", "wait")
    workflow.add_edge("wait", "review")
    workflow.add_conditional_edges("review", route_after_review,
        {"end": END, "retry": "select", "fail": END})
    return workflow.compile()


async def run_langgraph_batch(shots: list[dict], config: dict,
                               registry: TemplateRegistry,
                               client: ComfyUIClient = None) -> list[dict]:
    graph = build_graph()
    ctx = {"registry": registry, "client": client}
    results = []
    log.info(Msg.LG_BATCH_START.format(count=len(shots)))
    for shot in shots:
        init_state = {
            "id": shot.get("id", ""), "scene_desc": shot.get("scene_desc", ""),
            "dialogue": shot.get("dialogue", ""), "screen_text": shot.get("screen_text", ""),
            "asset_type": shot.get("asset_type", ""), "asset_path": shot.get("asset_path", ""),
            "duration": shot.get("duration", config.get("input", {}).get("default_duration", 5)), "config": config, "_ctx": ctx,
            "retry_count": 0, "max_retries": config.get("agent", {}).get("max_retries", 2),
        }
        log.info(f"── 镜号 {init_state['id']} ──")
        try:
            final_state = await graph.ainvoke(init_state)
        except Exception as e:
            log.error(f"镜号 {init_state['id']} 图执行失败: {e}")
            final_state = {**init_state, "status": "failed", "error_message": str(e)}
        result = {k: v for k, v in final_state.items() if k != "_ctx"}
        results.append(result)
    done = sum(1 for r in results if r.get("status") == "done")
    failed = sum(1 for r in results if r.get("status") == "failed")
    ffmpeg = sum(1 for r in results if r.get("status") == "pending_ffmpeg")
    log.info(Msg.LG_BATCH_DONE.format(done=done, failed=failed, ffmpeg=ffmpeg))
    return results