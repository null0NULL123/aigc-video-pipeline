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

    # 标准化字段
    shots = []
    for r in records:
        shot = {
            "id": str(r.get("id", "")).strip(),
            "duration": _parse_duration(str(r.get("时长", r.get("duration", "4")))),
            "scene_desc": str(r.get("画面内容", r.get("scene_desc", ""))).strip(),
            "dialogue": str(r.get("台词", r.get("dialogue", ""))).strip(),
            "screen_text": str(r.get("屏幕字幕", r.get("screen_text", ""))).strip(),
            "asset_type": str(r.get("素材来源", r.get("asset_type", "none"))).strip(),
            "asset_path": str(r.get("素材路径", r.get("asset_path", ""))).strip(),
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