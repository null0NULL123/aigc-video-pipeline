"""
媒体素材 API（视频 + 图片）
- GET    /api/videos                素材列表（生成媒体按 batch 分组 + 导入视频），?include_hidden=1 附带隐藏项
- POST   /api/videos/import         上传导入现有视频
- POST   /api/videos/hide           隐藏素材（软删除，文件保留）
- POST   /api/videos/unhide         恢复显示
- DELETE /api/videos?path=...       物理删除导入的视频（仅限 imported/）
- GET    /api/videos/{path:path}    媒体文件流（视频 mp4 + 图片，支持 Range）
- GET    /api/batches/{id}/download 批次产物打包下载（Zip，排除隐藏项）
"""
import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse

from web import settings

router = APIRouter(tags=["videos"])

ALLOWED_EXT = (".mp4", ".mov", ".m4v")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}
HIDDEN_FILE = "hidden.json"


def _hidden_paths() -> set[str]:
    """读取 output/hidden.json，返回隐藏媒体的相对路径集合"""
    f = settings.OUTPUT_DIR / HIDDEN_FILE
    try:
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
    except Exception:
        pass
    return set()


def _write_hidden(paths: set[str]):
    """写入隐藏列表；已不存在的文件自动清理（幽灵记录）"""
    out = settings.OUTPUT_DIR
    keep = set()
    for p in paths:
        fp = (out / p).resolve()
        if fp.is_file():
            keep.add(p)
    try:
        (out / HIDDEN_FILE).write_text(
            json.dumps(sorted(keep), ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _media_items() -> list[dict]:
    """扫描 output/ 下所有视频与图片（排除 assets/ 子树），标记 hidden"""
    if not settings.OUTPUT_DIR.exists():
        return []
    hidden = _hidden_paths()
    items = []
    for f in sorted(settings.OUTPUT_DIR.rglob("*")):
        if not f.is_file():
            continue
        suffix = f.suffix.lower()
        if suffix in ALLOWED_EXT:
            mtype = "video"
        elif suffix in IMAGE_EXT:
            mtype = "image"
        else:
            continue
        rel = f.relative_to(settings.OUTPUT_DIR)
        rel_str = str(rel).replace("\\", "/")
        parts = rel.parts
        if parts[0] == "assets":
            continue
        imported = rel_str.startswith("imported/")
        dialogue = ""
        if mtype == "video" and not imported:
            manifest_path = settings.OUTPUT_DIR / parts[0] / "generate_manifest.json"
            if manifest_path.exists():
                m = re.search(r"_shot_(\d+)\.mp4$", f.name)
                if m:
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        entry = manifest.get(str(m.group(1)))
                        if entry:
                            dialogue = entry.get("dialogue", "")
                    except Exception:
                        pass
        items.append({
            "filename": f.name,
            "path": rel_str,
            "batch_id": parts[0] if len(parts) > 1 else "root",
            "kind": "imported" if imported else "generated",
            "type": mtype,
            "dialogue": dialogue,
            "size_mb": round(f.stat().st_size / 1024 / 1024, 2),
            "modified": f.stat().st_mtime,
            "hidden": rel_str in hidden,
        })
    return items


@router.get("/videos")
async def list_videos(include_hidden: int = 0):
    """返回所有素材，按批次分组（导入视频归入「导入」组）；
    include_hidden=1 时额外返回 hidden 列表"""
    all_items = _media_items()
    hidden_items = [m for m in all_items if m["hidden"]]
    visible = [m for m in all_items if not m["hidden"]]

    batches = {}
    for v in visible:
        bid = "导入" if v["kind"] == "imported" else v["batch_id"]
        batches.setdefault(bid, []).append(v)
    for bid in batches:
        batches[bid].sort(key=lambda x: x["modified"], reverse=True)

    # 批次按最新文件时间倒序
    ordered = dict(sorted(batches.items(), key=lambda kv: max(x["modified"] for x in kv[1]), reverse=True))

    result = {"videos": visible, "batches": ordered, "total": len(visible), "hidden_total": len(hidden_items)}
    if include_hidden:
        result["hidden"] = hidden_items
    return result


@router.post("/videos/import")
async def import_video(file: UploadFile = File(...)):
    """上传视频到 output/imported/ 用于展示或后续合并"""
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXT:
        raise HTTPException(400, f"只支持 {', '.join(ALLOWED_EXT)} 文件")

    settings.IMPORTED_DIR.mkdir(parents=True, exist_ok=True)
    dest = settings.IMPORTED_DIR / f"{int(time.time())}_{Path(file.filename).name}"
    content = await file.read()
    if len(content) > 2 * 1024 * 1024 * 1024:
        raise HTTPException(400, "文件过大（超过 2GB）")
    dest.write_bytes(content)

    return {"ok": True, "message": f"已导入 {file.filename}",
            "path": str(dest.relative_to(settings.OUTPUT_DIR)).replace("\\", "/")}


def _check_media_path(path: str) -> Path:
    """校验相对路径合法且在 output/ 内，返回解析后路径"""
    out = settings.OUTPUT_DIR.resolve()
    file_path = (out / path).resolve()
    if not str(file_path).startswith(str(out)):
        raise HTTPException(400, "非法路径")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, f"文件不存在: {path}")
    return file_path


@router.post("/videos/hide")
async def hide_video(payload: dict):
    """隐藏素材（软删除）：从列表/合并/配音/打包中移除，文件保留"""
    path = str(payload.get("path", "")).strip()
    if not path:
        raise HTTPException(400, "缺少 path")
    _check_media_path(path)
    paths = _hidden_paths()
    paths.add(path)
    _write_hidden(paths)
    return {"ok": True, "message": f"已隐藏 {Path(path).name}"}


@router.post("/videos/unhide")
async def unhide_video(payload: dict):
    """恢复显示已隐藏的素材"""
    path = str(payload.get("path", "")).strip()
    if not path:
        raise HTTPException(400, "缺少 path")
    paths = _hidden_paths()
    if path not in paths:
        raise HTTPException(404, f"该素材不在隐藏列表: {path}")
    paths.discard(path)
    _write_hidden(paths)
    return {"ok": True, "message": f"已恢复显示 {Path(path).name}"}


@router.delete("/videos")
async def delete_video(path: str):
    """物理删除导入的视频（生成视频/图片请用 hide 隐藏）"""
    file_path = (settings.OUTPUT_DIR / path).resolve()
    imported_dir = settings.IMPORTED_DIR.resolve()
    if not str(file_path).startswith(str(imported_dir)):
        raise HTTPException(400, "只允许删除导入的视频")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, f"视频不存在: {path}")
    file_path.unlink()
    return {"ok": True, "message": f"已删除 {file_path.name}"}


@router.get("/batches/{batch_id}/download")
async def download_batch(batch_id: str):
    """打包下载批次产物（排除隐藏项）"""
    batch_dir = settings.OUTPUT_DIR / batch_id
    if not batch_dir.is_dir():
        raise HTTPException(404, f"批次不存在: {batch_id}")

    hidden = _hidden_paths()
    files = [f for f in sorted(batch_dir.rglob("*")) if f.is_file()]
    if hidden:
        files = [f for f in files
                 if str(f.relative_to(settings.OUTPUT_DIR)).replace("\\", "/") not in hidden]
    if not files:
        raise HTTPException(404, f"批次 {batch_id} 为空")

    fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix=f"{batch_id}_")
    os.close(fd)
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            arc = f"{batch_id}/{f.relative_to(batch_dir).as_posix()}"
            zf.write(f, arcname=arc)

    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"{batch_id}.zip",
        background=_cleanup(tmp_path),
    )


def _cleanup(path: str):
    from starlette.background import BackgroundTask

    def _rm():
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    return BackgroundTask(_rm)


@router.get("/videos/{path:path}")
async def stream_video(path: str, request: Request):
    """媒体文件流（视频 mp4 + 图片），支持 Range 请求（拖进度条/分段加载）"""
    file_path = settings.OUTPUT_DIR / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, f"文件不存在: {path}")

    suffix = file_path.suffix.lower()
    if suffix == ".mp4":
        media_type = "video/mp4"
    elif suffix in IMAGE_EXT:
        media_type = IMAGE_MIME.get(suffix, "application/octet-stream")
    else:
        raise HTTPException(400, f"不支持的文件类型: {suffix}")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        # 解析 Range: bytes=start-end | bytes=start- | bytes=-suffix
        range_spec = range_header.replace("bytes=", "").split("-")
        if range_spec[0]:
            start = int(range_spec[0])
            end = int(range_spec[1]) if range_spec[1] else file_size - 1
        else:
            suffix_n = int(range_spec[1])
            start = max(file_size - suffix_n, 0)
            end = file_size - 1
        end = min(end, file_size - 1)
        content_length = end - start + 1

        def iter_file():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iter_file(),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
            },
        )
    else:
        def iter_file():
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk

        return StreamingResponse(
            iter_file(),
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
            },
        )