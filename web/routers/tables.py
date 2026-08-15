"""
表格管理 API
- 表格 CRUD：GET/POST/PUT/DELETE /api/tables
- 镜头 CRUD（按表格）：/api/tables/{tid}/shots[...]
- 导入/导出：POST import / GET export / PUT reorder
首次启动自动迁移旧 input/web_shots.json 为「默认表格」。
"""
import io
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse

from web import settings

router = APIRouter(tags=["tables"])


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _migrate_shot(shot: dict) -> dict:
    """补齐新素材模型字段（assets 列表 / first_frame / last_frame）；
    旧 asset_type+asset_path 迁移为 assets[0]"""
    if "assets" not in shot:
        assets = []
        legacy_type = shot.get("asset_type", "")
        legacy_path = shot.get("asset_path", "")
        if legacy_type in ("image", "local") and legacy_path:
            assets.append({"type": "image", "path": legacy_path})
        elif legacy_path:
            ext = Path(legacy_path).suffix.lower()
            if ext in (".mp4", ".mov", ".m4v"):
                assets.append({"type": "video", "path": legacy_path})
            elif ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
                assets.append({"type": "image", "path": legacy_path})
            else:
                assets.append({"type": "text", "content": legacy_path})
        shot["assets"] = assets
    shot.setdefault("first_frame", "")
    shot.setdefault("last_frame", "")
    shot.setdefault("asset_type", "ai_generated")
    shot.setdefault("asset_path", "")
    return shot


def _ensure_tables() -> list[dict]:
    settings.TABLES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if settings.TABLES_FILE.exists():
        with open(settings.TABLES_FILE, encoding="utf-8") as f:
            tables = json.load(f)
    else:
        tables = None

    if tables is None:
        tables = []
        old = settings.SHOTS_FILE
        if old.exists():
            try:
                shots = json.loads(old.read_text(encoding="utf-8"))
                if shots:
                    tables.append({
                        "id": "1", "name": "默认表格",
                        "created_at": _now(), "shots": shots,
                    })
                    old.unlink(missing_ok=True)
            except Exception:
                pass
        _save_tables(tables)
        return tables

    changed = False
    for t in tables:
        for s in t.get("shots", []):
            if not isinstance(s, dict):
                continue
            before = json.dumps(s, sort_keys=True)
            s = _migrate_shot(s)
            if json.dumps(s, sort_keys=True) != before:
                changed = True
    if changed:
        _save_tables(tables)
    return tables


def _save_tables(tables: list[dict]):
    """原子写入，避免后台线程与 API 读取并发时读到半截文件"""
    settings.TABLES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.TABLES_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tables, f, ensure_ascii=False, indent=2)
    tmp.replace(settings.TABLES_FILE)


def _find_table(tables: list[dict], tid: str) -> dict:
    for t in tables:
        if str(t.get("id")) == str(tid):
            return t
    raise HTTPException(404, f"表格 {tid} 不存在")


def _next_id(items: list[dict]) -> str:
    ids = [int(s.get("id", 0)) for s in items if str(s.get("id", "")).isdigit()]
    return str(max(ids, default=0) + 1)


# ── 表格 CRUD ────────────────────────────────────────────

@router.get("/tables")
async def list_tables():
    tables = _ensure_tables()
    out = []
    for t in tables:
        shots = t.get("shots", [])
        out.append({
            "id": t["id"], "name": t.get("name", ""),
            "created_at": t.get("created_at", ""),
            "shot_count": len(shots),
            "done_count": sum(1 for s in shots if s.get("status") == "done"),
        })
    return out


@router.post("/tables")
async def create_table(body: dict):
    tables = _ensure_tables()
    table = {
        "id": _next_id(tables), "name": (body.get("name") or "新表格").strip(),
        "created_at": _now(), "shots": [],
    }
    tables.append(table)
    _save_tables(tables)
    return table


@router.put("/tables/{tid}")
async def rename_table(tid: str, body: dict):
    tables = _ensure_tables()
    t = _find_table(tables, tid)
    if body.get("name"):
        t["name"] = body["name"].strip()
    _save_tables(tables)
    return t


@router.delete("/tables/{tid}")
async def delete_table(tid: str):
    tables = _ensure_tables()
    before = len(tables)
    tables = [t for t in tables if str(t.get("id")) != str(tid)]
    if len(tables) == before:
        raise HTTPException(404, f"表格 {tid} 不存在")
    _save_tables(tables)
    return {"ok": True, "message": f"表格 {tid} 已删除"}


# ── 镜头 CRUD ────────────────────────────────────────────

@router.get("/tables/{tid}/shots")
async def list_shots(tid: str):
    tables = _ensure_tables()
    t = _find_table(tables, tid)
    return t.get("shots", [])


@router.post("/tables/{tid}/shots")
async def create_shot(tid: str, shot: dict):
    tables = _ensure_tables()
    t = _find_table(tables, tid)
    shots = t.setdefault("shots", [])
    shot["id"] = _next_id(shots)
    shot.setdefault("duration", 4)
    shot.setdefault("scene_desc", "")
    shot.setdefault("dialogue", "")
    shot.setdefault("screen_text", "")
    shot.setdefault("asset_type", "ai_generated")
    shot.setdefault("asset_path", "")
    shot.setdefault("workflow_id", "")
    shot.setdefault("status", "pending")
    _migrate_shot(shot)
    shots.append(shot)
    _save_tables(tables)
    return shot


@router.put("/tables/{tid}/shots/reorder")
async def reorder_shots(tid: str, body: dict):
    """body: {"ordered_ids": ["3","1","2",...]}"""
    tables = _ensure_tables()
    t = _find_table(tables, tid)
    ordered_ids = body.get("ordered_ids", [])
    shots = t.get("shots", [])
    shot_map = {str(s["id"]): s for s in shots}
    reordered = []
    for sid in ordered_ids:
        if str(sid) in shot_map:
            reordered.append(shot_map.pop(str(sid)))
    reordered.extend(shot_map.values())
    t["shots"] = reordered
    _save_tables(tables)
    return {"ok": True, "count": len(reordered)}


@router.put("/tables/{tid}/shots/{shot_id}")
async def update_shot(tid: str, shot_id: str, data: dict):
    tables = _ensure_tables()
    t = _find_table(tables, tid)
    for i, s in enumerate(t.get("shots", [])):
        if str(s.get("id")) == str(shot_id):
            data["id"] = str(shot_id)
            t["shots"][i] = data
            _save_tables(tables)
            return data
    raise HTTPException(404, f"镜头 {shot_id} 不存在")


@router.delete("/tables/{tid}/shots/{shot_id}")
async def delete_shot(tid: str, shot_id: str):
    tables = _ensure_tables()
    t = _find_table(tables, tid)
    shots = [s for s in t.get("shots", []) if str(s.get("id")) != str(shot_id)]
    if len(shots) == len(t.get("shots", [])):
        raise HTTPException(404, f"镜头 {shot_id} 不存在")
    t["shots"] = shots
    _save_tables(tables)
    return {"ok": True, "message": f"镜头 {shot_id} 已删除"}


# ── 导入导出 ─────────────────────────────────────────────

@router.post("/tables/{tid}/import")
async def import_shots(tid: str, file: UploadFile = File(...)):
    """从 CSV/XLSX 导入镜头（覆盖表格现有镜头）"""
    tables = _ensure_tables()
    t = _find_table(tables, tid)
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".csv", ".xlsx", ".xls"):
        raise HTTPException(400, "只支持 .csv / .xlsx 文件")

    content = await file.read()
    buf = io.BytesIO(content)

    if suffix == ".csv":
        df = pd.read_csv(buf, dtype=str)
    else:
        df = pd.read_excel(buf, dtype=str)

    df.columns = [c.strip().replace(" ", "_") for c in df.columns]

    col_map = {
        "时长": "duration",
        "画面内容": "scene_desc",
        "台词": "dialogue",
        "屏幕字幕": "screen_text",
        "嵌入文字": "screen_text",
        "素材来源": "asset_type",
        "素材路径": "asset_path",
        "文本素材": "text_asset",
        "首帧": "first_frame",
        "尾帧": "last_frame",
        "工作流": "workflow_id",
    }
    df.rename(columns=col_map, inplace=True)

    def _clean(v, default: str = "") -> str:
        s = str(v).strip()
        return "" if s in ("", "nan") else (s if s else default)

    shots = []
    for _, row in df.iterrows():
        dur = row.get("duration")
        try:
            duration = int(float(str(dur).replace("s", "").split("-")[-1])) if dur else 4
        except ValueError:
            duration = 4

        asset_path = _clean(row.get("asset_path"))
        assets = []
        for p in asset_path.split(";") if asset_path else []:
            p = p.strip()
            if not p:
                continue
            ext = Path(p).suffix.lower()
            if ext in (".mp4", ".mov", ".m4v"):
                assets.append({"type": "video", "path": p})
            elif ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
                assets.append({"type": "image", "path": p})
            else:
                assets.append({"type": "text", "content": p})
        txt = _clean(row.get("text_asset"))
        if txt:
            assets.append({"type": "text", "content": txt})

        shot = {
            "id": _clean(row.get("id")),
            "duration": duration,
            "scene_desc": _clean(row.get("scene_desc")),
            "dialogue": _clean(row.get("dialogue")),
            "screen_text": _clean(row.get("screen_text")),
            "asset_type": _clean(row.get("asset_type"), "ai_generated") or "ai_generated",
            "asset_path": asset_path,
            "assets": assets,
            "first_frame": _clean(row.get("first_frame")),
            "last_frame": _clean(row.get("last_frame")),
            "workflow_id": _clean(row.get("workflow_id")),
            "status": "pending",
        }
        _migrate_shot(shot)
        if shot["id"]:
            shots.append(shot)

    t["shots"] = shots
    _save_tables(tables)
    return {"ok": True, "message": f"导入 {len(shots)} 个镜头", "count": len(shots)}


@router.get("/tables/{tid}/export")
async def export_shots(tid: str):
    tables = _ensure_tables()
    t = _find_table(tables, tid)
    shots = t.get("shots", [])
    if not shots:
        raise HTTPException(404, "没有镜头可导出")

    rows = []
    for s in shots:
        assets = s.get("assets") or []
        paths = [a.get("path") for a in assets if a.get("path")]
        texts = [a.get("content") for a in assets
                 if a.get("type") == "text" and a.get("content")]
        rows.append({
            "id": s.get("id"),
            "时长": s.get("duration", 4),
            "画面内容": s.get("scene_desc", ""),
            "台词": s.get("dialogue", ""),
            "嵌入文字": s.get("screen_text", ""),
            "素材来源": s.get("asset_type", "ai_generated"),
            "素材路径": ";".join(paths),
            "文本素材": "\n".join(texts),
            "首帧": s.get("first_frame", ""),
            "尾帧": s.get("last_frame", ""),
            "工作流": s.get("workflow_id", ""),
        })
    df = pd.DataFrame(rows)

    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)

    filename = f"{t.get('name', 'table')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ── 状态回写（供生成完成后调用）──────────────────────────

def mark_shots_done(done: list[tuple[str, str]]):
    """把 (table_id, shot_id) 对应镜头标记为 done"""
    tables = _ensure_tables()
    changed = False
    wanted = {str(tid): set() for tid, _ in done}
    for tid, sid in done:
        wanted.setdefault(str(tid), set()).add(str(sid))
    for t in tables:
        ids = wanted.get(str(t.get("id")), set())
        if not ids:
            continue
        for s in t.get("shots", []):
            if str(s.get("id")) in ids and s.get("status") != "done":
                s["status"] = "done"
                changed = True
    if changed:
        _save_tables(tables)
