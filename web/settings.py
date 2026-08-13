"""
web 层统一配置：路径与常量集中管理
所有路径锚定项目根目录（BASE_DIR），避免依赖进程 CWD。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ── 路径 ──────────────────────────────────────────────────
CONFIG_PATH = BASE_DIR / "config.yaml"
BACKUP_PATH = BASE_DIR / "config.yaml.bak"
TEMPLATE_DIR = BASE_DIR / "templates"
SHOTS_FILE = BASE_DIR / "input" / "web_shots.json"
TABLES_FILE = BASE_DIR / "input" / "tables.json"
IMPORTED_DIR = BASE_DIR / "output" / "imported"
OUTPUT_DIR = BASE_DIR / "output"
STATIC_DIR = BASE_DIR / "web" / "static"
PIPELINE_CWD = BASE_DIR

# 盘符从项目所在盘推导，不硬编码
DISK_DRIVE = os.path.splitdrive(str(BASE_DIR))[0] or "C:\\"

# ── 默认值 ────────────────────────────────────────────────
COMFYUI_DEFAULT_HOST = "http://127.0.0.1:8188"
PIPELINE_DEFAULT_INPUT = "input/shots.csv"