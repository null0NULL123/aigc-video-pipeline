"""
日志配置
终端输出 INFO 级别，文件输出 DEBUG 级别
"""
import logging
import sys
from pathlib import Path


def _safe_stdout(stream):
    """Windows 控制台默认 GBK，无法编码 ✅/❌ 等字符，统一转 UTF-8 并容错"""
    try:
        if stream.encoding and stream.encoding.lower() in ("gbk", "cp936"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    return stream


def setup_logging(log_dir: str = "output/logs", level: str = "INFO"):
    """
    配置全局日志：
    - 控制台：INFO 级别，简洁格式
    - 文件：DEBUG 级别，完整格式，按日期滚动
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # 根 logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 清除已有 handler（避免重复）
    root.handlers.clear()

    # 控制台 handler
    console = logging.StreamHandler(_safe_stdout(sys.stdout))
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(
        "%(message)s"
    ))
    root.addHandler(console)

    # 文件 handler（单文件，追加模式）
    file_handler = logging.FileHandler(
        log_path / "pipeline.log",
        encoding="utf-8",
        mode="a",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(file_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger"""
    return logging.getLogger(name)