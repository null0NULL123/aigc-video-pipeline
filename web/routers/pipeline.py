"""
流水线控制 API
- POST /api/pipeline/run       启动全量生成（subprocess 调 cli.py，含合并）
- POST /api/generate           批量生成选中的镜头（可跨表格，只生成不合并）
- GET  /api/pipeline/status    当前运行状态
- GET  /api/pipeline/logs      最近日志（读 pipeline.log 尾部）
"""
import csv
import json
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException

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
}


def _start_run(cmd: list[str], job: str = None, on_done=None) -> None:
    """启动后台 subprocess，写入共享状态；on_done 在结束时回调"""
    _state.update({
        "running": True, "pid": None,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None, "exit_code": None, "last_output": "",
        "job": job,
    })

    def _run():
        exit_code = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(settings.PIPELINE_CWD),
                encoding="utf-8", errors="replace",
            )
            _state["pid"] = proc.pid
            output_lines = []
            for line in proc.stdout:
                output_lines.append(line.rstrip())
                _state["last_output"] = "\n".join(output_lines[-200:])
            proc.wait()
            exit_code = proc.returncode
            _state["exit_code"] = exit_code
        except Exception as e:
            _state["last_output"] = f"ERROR: {e}"
            exit_code = -1
            _state["exit_code"] = -1
        finally:
            _state["running"] = False
            _state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if on_done:
                try:
                    on_done(exit_code)
                except Exception:
                    pass

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


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
                rows.append({
                    "id": str(temp_id),
                    "时长": s.get("duration", 4),
                    "画面内容": s.get("scene_desc", ""),
                    "台词": s.get("dialogue", ""),
                    "屏幕字幕": s.get("screen_text", ""),
                    "素材来源": s.get("asset_type", "ai_generated"),
                    "素材路径": s.get("asset_path", ""),
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
        writer = csv.DictWriter(f, fieldnames=["id", "时长", "画面内容", "台词", "屏幕字幕", "素材来源", "素材路径"])
        writer.writeheader()
        writer.writerows(rows)
    return str(csv_path), meta


def _write_manifest(batch_id: str, meta: list[dict]):
    """把 temp_id → 镜头信息映射写入批次目录，供前端自动带出台词"""
    out_dir = Path(settings.OUTPUT_DIR) / batch_id
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {str(m["temp_id"]): m for m in meta}
    (out_dir / "generate_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _mark_done_from_batch(batch_id: str, meta: list[dict]):
    """扫描 output/{batch_id}/shots/*_shot_{temp_id}.mp4，回写表格状态"""
    shots_dir = Path(settings.OUTPUT_DIR) / batch_id / "shots"
    if not shots_dir.exists():
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

    _start_run(cmd, job="run")
    return {"ok": True, "message": "流水线已启动", "started_at": _state["started_at"]}


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

    _start_run(cmd, job=f"generate:{batch_name}", on_done=_on_done)
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
