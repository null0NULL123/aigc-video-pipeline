"""
图片素材库 API
- POST   /api/assets/images           上传图片素材
- GET    /api/assets/images           素材列表
- DELETE /api/assets/images?path=...  删除素材
- GET    /api/assets/images/{path}    图片文件流（供预览）
"""
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse

from web import settings

router = APIRouter(tags=["assets"])

ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
MAX_SIZE = 50 * 1024 * 1024  # 50MB


def _anchor() -> Path:
    """路径基准：生产环境=项目根；测试（OUTPUT_DIR 被重定向）=临时目录父级"""
    base = settings.BASE_DIR.resolve()
    out = settings.OUTPUT_DIR.resolve()
    if str(out).startswith(str(base)):
        return base
    return out.parent


def _rel_root(path: Path) -> str:
    """相对路径（generator 以 CWD=项目根 解析 asset_path）"""
    return str(path.relative_to(_anchor())).replace("\\", "/")


def _list_images() -> list[dict]:
    if not settings.ASSETS_DIR.exists():
        return []
    images = []
    for f in sorted(settings.ASSETS_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in ALLOWED_EXT:
            images.append({
                "name": f.name,
                "path": _rel_root(f),
                "size_mb": round(f.stat().st_size / 1024 / 1024, 2),
                "modified": f.stat().st_mtime,
            })
    return images


@router.post("/assets/images")
async def upload_image(file: UploadFile = File(...)):
    """上传图片到 output/assets/images/"""
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXT:
        raise HTTPException(400, f"只支持 {', '.join(ALLOWED_EXT)} 文件")

    settings.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    dest = settings.ASSETS_DIR / f"{int(time.time())}_{Path(file.filename).name}"
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(400, "文件过大（超过 50MB）")
    dest.write_bytes(content)

    return {"ok": True, "message": f"已上传 {file.filename}", "path": _rel_root(dest)}


@router.get("/assets/images")
async def list_images():
    return {"images": _list_images(), "total": len(_list_images())}


@router.delete("/assets/images")
async def delete_image(path: str):
    """删除素材库图片（仅限 assets 目录内）"""
    file_path = (_anchor() / path).resolve()
    assets_dir = settings.ASSETS_DIR.resolve()
    if not str(file_path).startswith(str(assets_dir)):
        raise HTTPException(400, "只允许删除素材库图片")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, f"图片不存在: {path}")
    file_path.unlink()
    return {"ok": True, "message": f"已删除 {file_path.name}"}


@router.get("/assets/images/{path:path}")
async def stream_image(path: str, request: Request):
    """图片文件流（支持 Range，供 <img>/<video> 预览）"""
    file_path = (_anchor() / path).resolve()
    if not str(file_path).startswith(str(settings.ASSETS_DIR.resolve())):
        raise HTTPException(400, "非素材库图片")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, f"图片不存在: {path}")

    suffix = file_path.suffix.lower()
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    }.get(suffix, "application/octet-stream")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    def iter_file(start: int, length: int):
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    if range_header:
        spec = range_header.replace("bytes=", "").split("-")
        if spec[0]:
            start = int(spec[0])
            end = int(spec[1]) if spec[1] else file_size - 1
        else:
            suffix_n = int(spec[1])
            start = max(file_size - suffix_n, 0)
            end = file_size - 1
        end = min(end, file_size - 1)
        return StreamingResponse(
            iter_file(start, end - start + 1),
            status_code=206, media_type=mime,
            headers={"Content-Range": f"bytes {start}-{end}/{file_size}", "Accept-Ranges": "bytes"},
        )
    return StreamingResponse(iter_file(0, file_size), media_type=mime,
                             headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)})