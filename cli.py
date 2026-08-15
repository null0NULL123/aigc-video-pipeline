"""
视频批量生产流水线 — 主入口（异步版）
输入→LangGraph Agent→合并
"""
import argparse, asyncio, os, sys
import yaml
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.input_reader import read_shots
from pipeline.generator import run_batch, set_current_batch_id
from pipeline.comfyui import ComfyUIClient
from pipeline.registry import TemplateRegistry
from pipeline import merge as merge_pipeline

from pipeline.log import get_logger
from pipeline.messages import Msg
log = get_logger("cli")



def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_dirs(config: dict):
    batch_id = config.get("_batch_id", "default")
    task_cfg = config.get("task", {})
    structure = task_cfg.get("output_structure", {})
    config.setdefault("output", {})
    if structure:
        config["output"]["shots_dir"] = structure.get("shots", "output/{batch_id}/shots").format(batch_id=batch_id)
        config["output"]["audio_dir"] = structure.get("audio", "output/{batch_id}/audio").format(batch_id=batch_id)
        config["output"]["subs_dir"] = structure.get("subs", "output/{batch_id}/subs").format(batch_id=batch_id)
        config["output"]["final_dir"] = structure.get("final", "output/{batch_id}/final").format(batch_id=batch_id)
    for key in ("shots_dir", "audio_dir", "subs_dir", "merged_dir", "final_dir"):
        path = config.get("output", {}).get(key, "")
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)


def find_latest_batch(output_base: str = "output") -> str:
    """找最新的 batch_id"""
    base = Path(output_base)
    if not base.exists():
        return ""
    batches = sorted([d.name for d in base.iterdir() if d.is_dir() and d.name.startswith("batch_")])
    return batches[-1] if batches else ""


def load_existing_results(batch_id: str, config: dict) -> list[dict]:
    """从已有 batch 目录恢复结果"""
    shots_dir = Path(config["output"]["shots_dir"])
    results = []
    if not shots_dir.exists():
        return results
    for mp4 in sorted(shots_dir.glob("*.mp4")):
        # 从文件名提取 shot_id: batch_xxx_shot_1.mp4 → 1
        name = mp4.stem
        parts = name.split("_")
        shot_id = parts[-1] if parts else "?"
        results.append({
            "shot_id": shot_id, "status": "done",
            "video_path": str(mp4),
        })
    return results


async def async_main(args):
    config = load_config(args.config)
    from pipeline.log import setup_logging
    setup_logging(
        log_dir=config.get("logging", {}).get("dir", "output/logs"),
        level=config.get("logging", {}).get("level", "INFO"),
    )

    # 确定 batch_id
    if args.name:
        # 用户命名
        batch_id = args.name
        batch_dir = Path("output") / batch_id
        if batch_dir.exists():
            existing_mp4s = list((batch_dir / "shots").glob("*.mp4")) if (batch_dir / "shots").exists() else []
            log.warning(f"批次 '{batch_id}' 已存在（{len(existing_mp4s)} 个视频），将追加/覆盖")
    elif args.batch_id:
        # 重试模式：找最新 batch
        batch_id = find_latest_batch()
        if not batch_id:
            log.error("没有找到已有批次，无法重试")
            return
    else:
        batch_prefix = config.get("task", {}).get("batch_prefix", "batch")
        batch_id = f"{batch_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    config["_batch_id"] = batch_id
    set_current_batch_id(batch_id)
    ensure_dirs(config)
    log.info(f"批次编号: {batch_id}")

    # ======== 读取脚本 ========
    log.info(Msg.MAIN_INPUT)
    shots = read_shots(args.input)
    registry = TemplateRegistry("templates", config=config)

    # ======== 重试模式 ========
    if args.retry:
        retry_ids = [s.strip() for s in args.retry.split(",")]
        log.info(f"重试镜头: {retry_ids}")

        # 加载已有结果
        existing = load_existing_results(batch_id, config)
        done_ids = {r["shot_id"] for r in existing if r.get("status") == "done"}
        log.info(f"已有完成: {done_ids}")

        # 只重试指定镜头（去掉已完成的）
        retry_shots = [s for s in shots if str(s.get("id")) in retry_ids and str(s.get("id")) not in done_ids]
        if not retry_shots:
            log.info("所有重试镜头已完成，跳过生成")
            shot_results = existing
        else:
            log.info(f"需要重试: {[s['id'] for s in retry_shots]}")
            async with ComfyUIClient(
                host=config["comfyui"]["host"],
                timeout=config["comfyui"].get("timeout", 600),
                poll_interval=config["comfyui"].get("poll_interval", 5),
            ) as client:
                new_results = await run_batch(retry_shots, config, registry, client,
                                              max_concurrency=config.get("agent", {}).get("concurrency", 2))
            # 合并已有 + 新结果
            shot_results = [r for r in existing if r.get("status") == "done"] + new_results
            log.info(f"合并后: {len(shot_results)} 个镜头")

    # ======== 正常模式 ========
    elif args.skip_gen:
        log.warning(Msg.MAIN_SKIP_GEN)
        shot_results = load_existing_results(batch_id, config)
        log.info(Msg.MAIN_RESTORE.format(count=len(shot_results)))
    else:
        async with ComfyUIClient(
            host=config["comfyui"]["host"],
            timeout=config["comfyui"].get("timeout", 600),
            poll_interval=config["comfyui"].get("poll_interval", 5),
        ) as client:
            shot_results = await run_batch(shots, config, registry, client,
                                           max_concurrency=config.get("agent", {}).get("concurrency", 2))

    # ======== 合并 ========
    if not args.skip_merge and shot_results:
        final_path = merge_pipeline.run(shot_results, config)
        log.info("=" * 40)
        log.info(Msg.MAIN_FINAL.format(path=final_path))
        log.info("=" * 40)
    else:
        log.warning(Msg.MAIN_SKIP_MERGE)
        log.info("当前状态:")
        for r in shot_results:
            log.info(Msg.MAIN_STATUS.format(
                id=r.get("shot_id", "?"), status=r.get("status", "unknown")
            ))


def main():
    parser = argparse.ArgumentParser(description="视频批量生产流水线")
    parser.add_argument("--input", default="input/shots.csv",
                        help="镜头脚本 xlsx/csv 路径")
    parser.add_argument("--config", default="config.yaml",
                        help="配置文件路径")
    parser.add_argument("--batch-id", default=None,
                        help="指定批次编号（默认自动生成）")
    parser.add_argument("--name", default=None,
                        help="为批次命名（如 promo_v1），已存在则提示")
    parser.add_argument("--retry", default=None,
                        help="重试指定镜头，逗号分隔（如 10,14）")
    parser.add_argument("--skip-gen", action="store_true",
                        help="跳过视频生成")
    parser.add_argument("--skip-merge", action="store_true",
                        help="跳过合并")
    args = parser.parse_args()

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()