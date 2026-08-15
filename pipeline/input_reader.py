"""
脚本输入
读取 Excel/CSV，每行 = 一个镜头
"""
import pandas as pd
from pathlib import Path

from pipeline.log import get_logger
from pipeline.messages import Msg
log = get_logger("reader")


def read_shots(excel_path: str) -> list[dict]:
    """
    读镜头脚本 Excel，返回 list[dict]
    预期列: id, duration, scene_desc, dialogue, screen_text, asset_type, asset_path
    """
    path = Path(excel_path)
    if not path.exists():
        raise FileNotFoundError(f"脚本文件不存在: {excel_path}")

    if path.suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    elif path.suffix == ".csv":
        df = pd.read_csv(path, dtype=str)
    else:
        raise ValueError(f"不支持的文件格式: {path.suffix}，请用 .xlsx 或 .csv")

    # 标准化列名
    df.columns = [c.strip().replace(" ", "_") for c in df.columns]

    # 按 id 排序
    if "id" in df.columns:
        df["id"] = df["id"].astype(str).str.strip()
        df["id_num"] = df["id"].str.extract(r"(\d+)").astype(float)
        df = df.sort_values("id_num").reset_index(drop=True)

    records = df.to_dict(orient="records")

    def _clean(value):
        """空单元格(dtype=str 会变成 'nan')归一化为空字符串"""
        text = str(value).strip()
        return "" if text.lower() in ("nan", "none") else text

    # 标准化字段
    shots = []
    for r in records:
        asset_path = _clean(r.get("素材路径", r.get("asset_path", "")))
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
        txt = _clean(r.get("文本素材", ""))
        if txt:
            assets.append({"type": "text", "content": txt})
        shot = {
            "id": str(r.get("id", "")).strip(),
            "duration": _parse_duration(_clean(r.get("时长", r.get("duration", "")))),
            "scene_desc": _clean(r.get("画面内容", r.get("scene_desc", ""))),
            "dialogue": _clean(r.get("台词", r.get("dialogue", ""))),
            "screen_text": _clean(r.get("屏幕字幕", r.get("screen_text", ""))),
            "asset_type": _clean(r.get("素材来源", r.get("asset_type", "none"))),
            "asset_path": asset_path,
            "assets": assets,
            "first_frame": _clean(r.get("首帧", "")),
            "last_frame": _clean(r.get("尾帧", "")),
            "workflow_id": _clean(r.get("工作流", "")),
            "status": "pending",
            "prompt": "",
            "video_path": "",
            "audio_path": "",
            "subs_path": "",
            "final_path": "",
        }
        shots.append(shot)

    log.info(Msg.INPUT_READ.format(count=len(shots), file=path.name))
    for s in shots:
        log.info(Msg.INPUT_SHOT.format(id=s["id"], dur=s["duration"], desc=s["scene_desc"][:30]))
    return shots


def _parse_duration(dur_str: str) -> int:
    """解析时长，'0-4s' → 4, '4-8s' → 4"""
    dur_str = dur_str.strip().lower().replace("s", "")
    if "-" in dur_str:
        parts = dur_str.split("-")
        try:
            start, end = float(parts[0]), float(parts[1])
            return int(round(end - start))
        except ValueError:
            return 5  # default from config.input.default_duration
    try:
        return int(float(dur_str))
    except ValueError:
        return 5  # default from config.input.default_duration