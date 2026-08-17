"""
流水线控制 API
- POST /api/pipeline/run       启动全量生成（subprocess 调 cli.py，含合并）
- POST /api/generate           批量生成选中的镜头（可跨表格，只生成不合并）
- POST /api/pipeline/batches/{id}/shots/{key}/redo  重做单个镜头
- POST /api/pipeline/batches/{id}/shots/{key}/confirm  标记已确认
- POST /api/pipeline/batches/{id}/shots/{key}/unconfirm  撤回确认
- GET  /api/pipeline/batches/{id}/shots             列出状态
- GET  /api/pipeline/batches/{id}/shots/{key}       单个详情
- GET  /api/pipeline/batches/{id}/events  SSE 推送
- POST /api/pipeline/batches/{id}/merge  只合并 confirmed 的
- GET  /api/pipeline/status    当前运行状态
- GET  /api/pipeline/logs      最近日志
"""
import asyncio
import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException

from pipeline.messages import Msg
from pipeline.tracker import get_tracker

from web import settings
from web.routers import tables as tables_router

router = APIRouter(tags=["pipeline"])

# 运行状态
_state = {
    "running": False,
    "pid": None,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "last_output": "",
    "job": None,
    "batch_id": None,
}


def _parse_marker_line(line: str) -> dict | None:
    """解析 stdout marker 行；非 marker 行返回 None"""
    s = line.strip()
    if not s.startswith(Msg.PIPE_EVENT_PREFIX):
        return None
    payload = s[len(Msg.PIPE_EVENT_PREFIX):].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _handle_marker(payload: dict) -> None:
    """根据 payload 更新 tracker"""
    tracker = get_tracker()
    batch_id = payload.get("batch_id") or ""
    event = payload.get("event") or ""
    shot_key = str(payload.get("shot_key") or "")
    if not batch_id:
        return

    tracker.ensure_batch(batch_id)

    if event == "batch_done":
        tracker.mark_done(batch_id, exit_code=payload.get("exit_code", 0))
        return

    if not shot_key:
        return

    # 透传字段给 tracker.update_shot（去掉 event/batch_id/shot_key/ts）
    fields = {k: v for k, v in payload.items()
              if k not in ("event", "batch_id", "shot_key", "ts")}
    tracker.update_shot(batch_id, shot_key, **fields)


async def _start_run_async(cmd: list[str], batch_id: str, job: str, on_done=None) -> None:
    """异步启动 subprocess；stdout 同时解析 marker 行（喂 tracker）和保留给前端日志"""
    tracker = get_tracker()
    tracker.ensure_batch(batch_id)
    _state.update({
        "running": True, "pid": None,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None, "exit_code": None, "last_output": "",
        "job": job, "batch_id": batch_id,
    })

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(settings.PIPELINE_CWD),
    )
    _state["pid"] = proc.pid

    output_lines: list[str] = []
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        try:
            line = raw.decode("utf-8", errors="replace").rstrip()
        except Exception:
            line = raw.decode("utf-8", errors="replace").rstrip()  # noqa
        # marker 行解析
        if line.lstrip().startswith(Msg.PIPE_EVENT_PREFIX):
            payload = _parse_marker_line(line)
            if payload:
                _handle_marker(payload)
            # 不写进 last_output（前端不显示）
            continue
        output_lines.append(line)
        _state["last_output"] = "\n".join(output_lines[-200:])

    exit_code = await proc.wait()
    _state["exit_code"] = exit_code
    _state["running"] = False
    _state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tracker.mark_done(batch_id, exit_code=exit_code)

    if on_done:
        try:
            on_done(exit_code)
        except Exception:
            pass


def _start_run(cmd: list[str], batch_id: str, job: str = None, on_done=None) -> None:
    """同步接口：触发 _start_run_async，fire-and-forget"""
    asyncio.create_task(_start_run_async(cmd, batch_id, job or "run", on_done))


def _gen_batch_name() -> str:
    return f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _export_selection_csv(selections: list[dict]) -> tuple[str, list[dict]]:
    """
    从各表格收集选中镜头 → 写临时 CSV（列格式与 pipeline.input_reader 一致）
    返回 (csv_path, meta)；meta: [{temp_id, table_id, shot_id, dialogue, screen_text}]
    """
    tables = tables_router._ensure_tables()
    table_map = {str(t.get("id")): t for t in tables}

    rows = []
    meta = []
    temp_id = 1
    for sel in selections:
        tid = str(sel.get("table_id", ""))
        table = table_map.get(tid)
        if not table:
            continue
        shot_ids = {str(s) for s in sel.get("shot_ids", [])}
        for s in table.get("shots", []):
            if str(s.get("id")) in shot_ids:
                assets = s.get("assets") or []
                paths = [a.get("path") for a in assets if a.get("path")]
                texts = [a.get("content") for a in assets
                         if a.get("type") == "text" and a.get("content")]
                rows.append({
                    "id": str(temp_id),
                    "时长": s.get("duration", 4),
                    "画面内容": s.get("scene_desc", ""),
                    "台词": s.get("dialogue", ""),
                    "屏幕字幕": s.get("screen_text", ""),
                    "素材来源": s.get("asset_type", "ai_generated"),
                    "素材路径": ";".join(paths),
                    "文本素材": "\n".join(texts),
                    "首帧": s.get("first_frame", ""),
                    "尾帧": s.get("last_frame", ""),
                    "工作流": s.get("workflow_id", ""),
                })
                meta.append({
                    "temp_id": temp_id,
                    "table_id": tid,
                    "shot_id": str(s.get("id")),
                    "dialogue": s.get("dialogue", ""),
                    "screen_text": s.get("screen_text", ""),
                })
                temp_id += 1

    if not rows:
        raise HTTPException(400, "没有选中的镜头")

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = settings.PIPELINE_CWD / "input" / f"tmp_gen_{ts}.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "时长", "画面内容", "台词", "屏幕字幕", "素材来源", "素材路径", "文本素材", "首帧", "尾帧", "工作流"])
        writer.writeheader()
        writer.writerows(rows)
    return str(csv_path), meta


def _write_manifest(batch_id: str, meta: list[dict]):
    """把 temp_id → 镜头信息映射写入批次目录，供前端自动带出台词

    新结构: output/shots/{batch_id}/generate_manifest.json（manifest 与 shots 关联）
    兼容: 旧结构 output/{batch_id}/generate_manifest.json
    """
    candidates = [
        Path(settings.OUTPUT_DIR) / "shots" / batch_id,        # 新结构
        Path(settings.OUTPUT_DIR) / batch_id,                  # 旧结构
    ]
    out_dir = next((d for d in candidates if d.exists()), candidates[0])
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {str(m["temp_id"]): m for m in meta}
    (out_dir / "generate_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _mark_done_from_batch(batch_id: str, meta: list[dict]):
    """扫描 batch 的 shots 目录（兼容新旧结构），回写表格状态

    新结构: output/shots/{batch_id}/*.mp4
    旧结构: output/{batch_id}/shots/*.mp4
    """
    candidates = [
        Path(settings.OUTPUT_DIR) / "shots" / batch_id,        # 新结构
        Path(settings.OUTPUT_DIR) / batch_id / "shots",        # 旧结构
    ]
    shots_dir = next((d for d in candidates if d.exists()), None)
    if not shots_dir:
        return
    done_pairs = []
    for mp4 in shots_dir.glob("*.mp4"):
        m = re.search(r"_shot_(\d+)\.mp4$", mp4.name)
        if m:
            temp_id = int(m.group(1))
            if 1 <= temp_id <= len(meta):
                done_pairs.append((meta[temp_id - 1]["table_id"], meta[temp_id - 1]["shot_id"]))
    if done_pairs:
        tables_router.mark_shots_done(done_pairs)


@router.post("/pipeline/run")
async def run_pipeline(body: dict = None):
    """启动 cli.py 全量生成（含合并）"""
    if _state["running"]:
        raise HTTPException(409, "流水线正在运行中，请等待完成")

    input_file = (body or {}).get("input", settings.PIPELINE_DEFAULT_INPUT)
    batch_name = (body or {}).get("name", "")
    retry_ids = (body or {}).get("retry", "")

    cmd = ["python", "cli.py", "--input", input_file]
    if batch_name:
        cmd += ["--name", batch_name]
    if retry_ids:
        cmd += ["--retry", retry_ids]

    if not batch_name:
        batch_name = _gen_batch_name()
        cmd += ["--name", batch_name]

    _start_run(cmd, batch_id=batch_name, job="run")
    return {"ok": True, "message": "流水线已启动", "started_at": _state["started_at"], "batch_id": batch_name}


@router.post("/generate")
async def generate_shots(body: dict = None):
    """
    批量生成选中的镜头（可跨表格）
    body: {"name": "可选批次名", "selections": [{"table_id": "1", "shot_ids": ["1","2"]}]}
    """
    if _state["running"]:
        raise HTTPException(409, "流水线正在运行中，请等待完成")

    selections = (body or {}).get("selections") or []
    batch_name = (body or {}).get("name", "").strip() or _gen_batch_name()

    csv_path, meta = _export_selection_csv(selections)

    cmd = ["python", "cli.py", "--input", csv_path, "--name", batch_name, "--skip-merge"]

    def _on_done(exit_code: int):
        if exit_code == 0:
            try:
                _mark_done_from_batch(batch_name, meta)
            except Exception:
                pass
        try:
            Path(csv_path).unlink(missing_ok=True)
        except Exception:
            pass

    _start_run(cmd, batch_id=batch_name, job=f"generate:{batch_name}", on_done=_on_done)
    _write_manifest(batch_name, meta)
    return {"ok": True, "message": f"开始生成 {len(meta)} 个镜头", "batch": batch_name,
            "started_at": _state["started_at"]}


@router.get("/pipeline/status")
async def pipeline_status():
    return {
        "running": _state["running"],
        "pid": _state["pid"],
        "started_at": _state["started_at"],
        "finished_at": _state["finished_at"],
        "exit_code": _state["exit_code"],
        "job": _state.get("job"),
        "output_lines": _state["last_output"].count("\n") + 1 if _state["last_output"] else 0,
    }


@router.get("/pipeline/logs")
async def pipeline_logs(tail: int = 100):
    """返回流水线实时输出（最近 N 行）"""
    lines = _state["last_output"].split("\n") if _state["last_output"] else []
    return {"lines": lines[-tail:], "total": len(lines), "running": _state["running"]}


@router.post("/images/generate")
async def generate_images(body: dict = None):
    """
    文生图工具：写单行 CSV → cli.py 跑 t2i 工作流
    body: {"prompt": "...", "count": N, "name": "可选批次名"}
    产物图片输出到 output/{batch}/shots/，自动出现在素材库
    """
    if _state["running"]:
        raise HTTPException(409, "流水线正在运行中，请等待完成")

    prompt = ((body or {}).get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "缺少提示词")
    count = max(1, min(int((body or {}).get("count", 1)), 10))
    batch_name = (body or {}).get("name", "").strip() or \
        f"img_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = settings.PIPELINE_CWD / "input" / f"tmp_img_{ts}.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "时长", "画面内容", "台词", "屏幕字幕", "素材来源", "素材路径", "文本素材", "首帧", "尾帧", "工作流"])
        writer.writeheader()
        for i in range(1, count + 1):
            writer.writerow({
                "id": str(i), "时长": 5, "画面内容": prompt,
                "台词": "", "屏幕字幕": "", "素材来源": "ai_generated",
                "素材路径": "", "文本素材": "", "首帧": "", "尾帧": "",
                "工作流": "seedream_t2i",
            })

    cmd = ["python", "cli.py", "--input", str(csv_path), "--name", batch_name, "--skip-merge"]

    def _on_done(exit_code: int):
        try:
            Path(csv_path).unlink(missing_ok=True)
        except Exception:
            pass

    _start_run(cmd, batch_id=batch_name, job=f"image:{batch_name}", on_done=_on_done)
    return {"ok": True, "message": f"开始生成 {count} 张图片", "batch": batch_name,
            "started_at": _state["started_at"]}
# ═══════════════════════════════════════════════════════════════
# 镜头 Review & 重做（基于 tracker）
# ═══════════════════════════════════════════════════════════════

@router.get("/pipeline/batches")
async def list_batches():
    """列出所有 batch（demo 用，无分页）"""
    return {"batches": get_tracker().list_batches_with_meta()}


@router.get("/pipeline/batches/{batch_id}/shots")
async def list_batch_shots(batch_id: str, status: str | None = None):
    """列出该 batch 的所有 shot 状态"""
    tracker = get_tracker()
    batch = tracker.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, f"批次 '{batch_id}' 不存在或已被清理")
    shots = tracker.list_shots(batch_id, status_filter=status)
    return {"batch_id": batch_id, "shots": [s.to_dict() for s in shots]}


@router.get("/pipeline/batches/{batch_id}/shots/{shot_key}")
async def get_batch_shot(batch_id: str, shot_key: str):
    tracker = get_tracker()
    shot = tracker.get_shot(batch_id, shot_key)
    if not shot:
        raise HTTPException(404, f"镜头 '{shot_key}' 不存在")
    return shot.to_dict()


@router.post("/pipeline/batches/{batch_id}/shots/{shot_key}/confirm")
async def confirm_shot(batch_id: str, shot_key: str):
    tracker = get_tracker()
    shot = tracker.get_shot(batch_id, shot_key)
    if not shot:
        raise HTTPException(404, f"镜头 '{shot_key}' 不存在")
    if shot.status not in ("done", "confirmed"):
        raise HTTPException(400, f"只能确认已完成的镜头（当前: {shot.status}）")
    tracker.mark_confirmed(batch_id, shot_key)
    return {"ok": True, "shot_key": shot_key, "status": "confirmed"}


@router.post("/pipeline/batches/{batch_id}/shots/{shot_key}/unconfirm")
async def unconfirm_shot(batch_id: str, shot_key: str):
    tracker = get_tracker()
    shot = tracker.get_shot(batch_id, shot_key)
    if not shot:
        raise HTTPException(404, f"镜头 '{shot_key}' 不存在")
    if shot.status != "confirmed":
        raise HTTPException(400, f"镜头未确认（当前: {shot.status}）")
    tracker.mark_unconfirmed(batch_id, shot_key)
    return {"ok": True, "shot_key": shot_key, "status": "done"}


@router.post("/pipeline/batches/{batch_id}/shots/{shot_key}/redo")
async def redo_shot(batch_id: str, shot_key: str):
    """
    重做单个镜头：读 output/{batch_id}/generate_manifest.json 找到 meta，
    调 cli.py --redo 单独跑这一个 shot。
    """
    if _state["running"]:
        raise HTTPException(409, "流水线正在运行中，请等待完成")

    manifest_path = Path(settings.OUTPUT_DIR) / batch_id / "generate_manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, f"批次 '{batch_id}' 没有 manifest（用 /api/generate 启动才会有）")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"manifest 解析失败: {e}")

    meta = manifest.get(str(shot_key))
    if not meta:
        raise HTTPException(404, f"manifest 里找不到 shot_key={shot_key}")

    # 重置 shot 状态为 pending，让前端看到进度
    get_tracker().update_shot(batch_id, shot_key,
                              status="pending",
                              error=None,
                              stage="redo",
                              video_path=None)

    # 写一个只含当前 shot 的临时 CSV
    tables = tables_router._ensure_tables()
    table_map = {str(t.get("id")): t for t in tables}
    tid = meta.get("table_id", "")
    sid = meta.get("shot_id", "")
    table = table_map.get(tid)
    s = next((x for x in (table or {}).get("shots", []) if str(x.get("id")) == sid), None)
    if not table or not s:
        raise HTTPException(404, f"镜头不在当前表格中（table={tid}, shot={sid}）")

    assets = s.get("assets") or []
    paths = [a.get("path") for a in assets if a.get("path")]
    texts = [a.get("content") for a in assets if a.get("type") == "text" and a.get("content")]
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = settings.PIPELINE_CWD / "input" / f"tmp_redo_{batch_id}_{shot_key}_{ts}.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "时长", "画面内容", "台词", "屏幕字幕",
            "素材来源", "素材路径", "文本素材", "首帧", "尾帧", "工作流"])
        writer.writeheader()
        writer.writerow({
            "id": shot_key,
            "时长": s.get("duration", 4),
            "画面内容": s.get("scene_desc", ""),
            "台词": meta.get("dialogue") or s.get("dialogue", ""),
            "屏幕字幕": meta.get("screen_text") or s.get("screen_text", ""),
            "素材来源": s.get("asset_type", "ai_generated"),
            "素材路径": ";".join(paths),
            "文本素材": "\n".join(texts),
            "首帧": s.get("first_frame", ""),
            "尾帧": s.get("last_frame", ""),
            "工作流": s.get("workflow_id", ""),
        })

    cmd = ["python", "cli.py", "--input", str(csv_path),
           "--name", batch_id, "--skip-merge"]

    def _on_done(_exit_code: int):
        try:
            csv_path.unlink(missing_ok=True)
        except Exception:
            pass

    _start_run(cmd, batch_id=batch_id, job=f"redo:{batch_id}:{shot_key}", on_done=_on_done)
    return {"ok": True, "message": f"开始重做 shot {shot_key}",
            "batch_id": batch_id, "shot_key": shot_key}


@router.post("/pipeline/batches/{batch_id}/merge")
async def merge_confirmed(batch_id: str, body: dict | None = None):
    """
    只合并 confirmed 状态的镜头。
    写个临时 CSV（只含 confirmed 的 shot），调 cli.py --skip-gen --name {batch_id}。
    """
    if _state["running"]:
        raise HTTPException(409, "流水线正在运行中，请等待完成")

    tracker = get_tracker()
    confirmed = tracker.list_shots(batch_id, status_filter="confirmed")
    if not confirmed:
        raise HTTPException(400, "没有已确认的镜头可以合并")

    manifest_path = Path(settings.OUTPUT_DIR) / batch_id / "generate_manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, f"批次 '{batch_id}' 没有 manifest")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"manifest 解析失败: {e}")

    name = (body or {}).get("name", "").strip() or _gen_batch_name()
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = settings.PIPELINE_CWD / "input" / f"tmp_merge_{name}_{ts}.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    tables = tables_router._ensure_tables()
    table_map = {str(t.get("id")): t for t in tables}

    rows = []
    temp_id = 1
    for shot_state in confirmed:
        key = shot_state.key
        meta = manifest.get(key)
        if not meta:
            continue
        tid = meta.get("table_id", "")
        sid = meta.get("shot_id", "")
        table = table_map.get(tid)
        if not table:
            continue
        s = next((x for x in table.get("shots", []) if str(x.get("id")) == sid), None)
        if not s:
            continue
        rows.append({
            "id": str(temp_id),
            "时长": s.get("duration", 4),
            "画面内容": s.get("scene_desc", ""),
            "台词": meta.get("dialogue") or s.get("dialogue", ""),
            "屏幕字幕": meta.get("screen_text") or s.get("screen_text", ""),
            "素材来源": s.get("asset_type", "ai_generated"),
            "素材路径": "",
            "文本素材": "",
            "首帧": s.get("first_frame", ""),
            "尾帧": s.get("last_frame", ""),
            "工作流": s.get("workflow_id", ""),
        })
        temp_id += 1

    if not rows:
        raise HTTPException(400, "确认的镜头无法定位到表格记录")

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "时长", "画面内容", "台词", "屏幕字幕",
            "素材来源", "素材路径", "文本素材", "首帧", "尾帧", "工作流"])
        writer.writeheader()
        writer.writerows(rows)

    cmd = ["python", "cli.py", "--input", str(csv_path),
           "--name", name, "--skip-gen"]

    def _on_done(_exit_code: int):
        try:
            csv_path.unlink(missing_ok=True)
        except Exception:
            pass

    _start_run(cmd, batch_id=name, job=f"merge:{name}", on_done=_on_done)
    return {"ok": True, "message": f"开始合并 {len(rows)} 个已确认镜头", "batch": name,
            "shot_count": len(rows), "started_at": _state["started_at"]}

# ═══════════════════════════════════════════════════════════════
# SSE 实时进度推送
# ═══════════════════════════════════════════════════════════════

@router.get("/pipeline/batches/{batch_id}/events")
async def batch_events(batch_id: str):
    """
    SSE 端点：前端订阅后，每条 shot 状态变更都会实时推送。

    事件格式：
      event: shot_update
      data: {"batch_id":"...", "shot_key":"...", "fields":{...}}

      event: batch_done
      data: {"batch_id":"...", "exit_code":0}
    """
    from starlette.responses import StreamingResponse
    import json

    tracker = get_tracker()
    queue = tracker.subscribe(batch_id)

    async def event_generator():
        """从 queue 读取事件，格式化为 SSE 格式"""
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    # 心跳：保持连接
                    yield ": heartbeat\n\n"
                    continue

                event_type = event.get("type", "unknown")
                data = json.dumps(event, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {data}\n\n"

                # batch_done 后结束
                if event_type == "batch_done":
                    break
        finally:
            tracker.unsubscribe(batch_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
