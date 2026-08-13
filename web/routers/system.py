"""
系统状态 API
- GET /api/system/status   ComfyUI 状态 + 磁盘空间 + 最近运行
"""
import shutil
import time
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


def _latest_batch() -> dict | None:
    """最近一次运行摘要"""
    if not settings.OUTPUT_DIR.exists():
        return None

    batches = sorted([d for d in settings.OUTPUT_DIR.iterdir() if d.is_dir() and d.name.startswith("batch_")])
    if not batches:
        return None

    latest = batches[-1]
    shots_dir = latest / "shots"
    shots_count = len(list(shots_dir.glob("*.mp4"))) if shots_dir.exists() else 0
    final = latest / "final" / "final_video.mp4"

    return {
        "batch_id": latest.name,
        "shots_count": shots_count,
        "has_final": final.exists(),
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
