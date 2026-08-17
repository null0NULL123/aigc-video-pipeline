"""
系统状态 API
- GET /api/system/status   ComfyUI 状态 + 磁盘空间 + 最近运行
"""
import shutil
import time
from pathlib import Path
import yaml
from datetime import datetime
from fastapi import APIRouter
import aiohttp
from web import settings

router = APIRouter(tags=["system"])


async def _check_comfyui(host: str) -> dict:
    """检测 ComfyUI 连接状态"""
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{host}/system_stats") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {"online": True, "host": host, "stats": data}
                return {"online": False, "host": host, "error": f"HTTP {resp.status}"}
    except Exception as e:
        return {"online": False, "host": host, "error": str(e)[:100]}


def _disk_usage() -> dict:
    """磁盘空间"""
    output_size = 0
    if settings.OUTPUT_DIR.exists():
        output_size = sum(f.stat().st_size for f in settings.OUTPUT_DIR.rglob("*") if f.is_file())

    # 项目所在盘空间
    try:
        total, used, free = shutil.disk_usage(settings.DISK_DRIVE)
        return {
            "output_size_mb": round(output_size / 1024 / 1024, 1),
            "disk_total_gb": round(total / 1024**3, 1),
            "disk_used_gb": round(used / 1024**3, 1),
            "disk_free_gb": round(free / 1024**3, 1),
            "disk_percent": round(used / total * 100, 1),
        }
    except Exception:
        return {"output_size_mb": round(output_size / 1024 / 1024, 1)}


CATEGORY_DIRS = ("shots", "merged", "final", "audio", "subs", "logs")


def _discover_batches() -> list[Path]:
    """发现所有 batch（兼容新旧结构）

    新结构: output/{category}/{batch_id}/...
    旧结构: output/{batch_id}/{category}/...
    """
    if not settings.OUTPUT_DIR.exists():
        return []
    found: dict[str, Path] = {}
    # 新结构：扫描 6 个分类目录
    for cat in CATEGORY_DIRS:
        cat_dir = settings.OUTPUT_DIR / cat
        if not cat_dir.exists():
            continue
        for d in cat_dir.iterdir():
            if d.is_dir():
                found.setdefault(d.name, d)
    # 旧结构：直接子目录里有分类子目录的（demo_xxx/batch_xxx）
    for d in settings.OUTPUT_DIR.iterdir():
        if not d.is_dir():
            continue
        if d.name in CATEGORY_DIRS or d.name.startswith("."):
            continue  # 跳过分类根
        # 判定：含 shots/final/audio/subs/merged 任一子目录
        if any((d / c).is_dir() for c in CATEGORY_DIRS):
            found.setdefault(d.name, d)
    # 按 mtime 倒序
    return sorted(found.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def _resolve_batch_paths(batch_dir: Path) -> tuple[Path | None, Path | None]:
    """解析 batch 的 shots_dir 和 final video 路径，兼容新旧结构"""
    shots_dir = final_path = None
    # 新结构
    for cat, role in (("shots", "shots_dir"), ("final", "final")):
        d = settings.OUTPUT_DIR / cat / batch_dir.name
        if d.is_dir():
            if role == "shots_dir":
                shots_dir = d
            elif role == "final":
                # 新结构 final dir 下找 final_video.mp4
                p = d / "final_video.mp4"
                if p.exists():
                    final_path = p
    # 旧结构（output/{batch_id}/{cat}/）
    for cat in ("shots",):
        d = batch_dir / cat
        if d.is_dir() and shots_dir is None:
            shots_dir = d
    final_legacy = batch_dir / "final" / "final_video.mp4"
    if final_legacy.exists() and final_path is None:
        final_path = final_legacy
    return shots_dir, final_path


def _latest_batch() -> dict | None:
    """最近一次运行摘要"""
    batches = _discover_batches()
    if not batches:
        return None
    latest = batches[0]
    shots_dir, final = _resolve_batch_paths(latest)
    shots_count = len(list(shots_dir.glob("*.mp4"))) if shots_dir and shots_dir.exists() else 0

    return {
        "batch_id": latest.name,
        "shots_count": shots_count,
        "has_final": final.exists() if final else False,
        "created_at": datetime.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/system/status")
async def system_status():
    # 读配置拿 comfyui host
    host = settings.COMFYUI_DEFAULT_HOST
    if settings.CONFIG_PATH.exists():
        with open(settings.CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        host = cfg.get("comfyui", {}).get("host", host)

    comfyui = await _check_comfyui(host)
    disk = _disk_usage()
    latest = _latest_batch()

    return {
        "comfyui": comfyui,
        "disk": disk,
        "latest_batch": latest,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
