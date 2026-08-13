"""
视频预览 API
- GET   /api/videos               视频列表（生成镜头按 batch 分组 + 导入视频）
- POST  /api/videos/import        上传导入现有视频
- DELETE /api/videos?path=...     删除导入的视频
- GET   /api/videos/{path:path}   视频文件流（支持 Range 请求）
"""
import json
import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse

from web import settings

router = APIRouter(tags=["videos"])

ALLOWED_EXT = (".mp4", ".mov", ".m4v")


def _scan_videos() -> list[dict]:
    """扫描 output/ 下所有视频，标记 kind：generated / imported，生成镜头附带台词"""
    if not settings.OUTPUT_DIR.exists():
        return []
    videos = []
    for vid in sorted(settings.OUTPUT_DIR.rglob("*")):
        if not vid.is_file() or vid.suffix.lower() not in ALLOWED_EXT:
            continue
        rel = vid.relative_to(settings.OUTPUT_DIR)
        parts = rel.parts
        imported = str(rel).replace("\\", "/").startswith("imported/")
        dialogue = ""
        if not imported:
            # 从批次 manifest 查找台词（temp_id → 镜头信息）
            manifest_path = settings.OUTPUT_DIR / parts[0] / "generate_manifest.json"
            if manifest_path.exists():
                m = re.search(r"_shot_(\d+)\.mp4$", vid.name)
                if m:
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        entry = manifest.get(str(m.group(1)))
                        if entry:
                            dialogue = entry.get("dialogue", "")
                    except Exception:
                        pass
        videos.append({
            "filename": vid.name,
            "path": str(rel).replace("\\", "/"),
            "batch_id": parts[0] if len(parts) > 1 else "root",
            "kind": "imported" if imported else "generated",
            "dialogue": dialogue,
            "size_mb": round(vid.stat().st_size / 1024 / 1024, 2),
            "modified": vid.stat().st_mtime,
        })
    return videos


@router.get("/videos")
async def list_videos():
    """返回所有视频，按批次分组（导入视频归入「导入」组）"""
    videos = _scan_videos()
    batches = {}
    for v in videos:
        bid = "导入" if v["kind"] == "imported" else v["batch_id"]
        batches.setdefault(bid, []).append(v)
    return {"videos": videos, "batches": batches, "total": len(videos)}


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


@router.delete("/videos")
async def delete_video(path: str):
    """删除导入的视频（生成视频不删除）"""
    file_path = (settings.OUTPUT_DIR / path).resolve()
    imported_dir = settings.IMPORTED_DIR.resolve()
    if not str(file_path).startswith(str(imported_dir)):
        raise HTTPException(400, "只允许删除导入的视频")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, f"视频不存在: {path}")
    file_path.unlink()
    return {"ok": True, "message": f"已删除 {file_path.name}"}


@router.get("/videos/{path:path}")
async def stream_video(path: str, request: Request):
    """视频文件流，支持 Range 请求（拖进度条）"""
    file_path = settings.OUTPUT_DIR / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, f"视频不存在: {path}")
    if not path.endswith(".mp4"):
        raise HTTPException(400, "只支持 .mp4 文件")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        # 解析 Range: bytes=start-end | bytes=start- | bytes=-suffix
        range_spec = range_header.replace("bytes=", "").split("-")
        if range_spec[0]:
            start = int(range_spec[0])
            end = int(range_spec[1]) if range_spec[1] else file_size - 1
        else:
            # 后缀范围 bytes=-N：取最后 N 字节
            suffix = int(range_spec[1])
            start = max(file_size - suffix, 0)
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
            media_type="video/mp4",
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
            media_type="video/mp4",
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
            },
        )
