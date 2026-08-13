"""
LLM 客户端 — 调用 OpenAI 兼容 API 优化 prompt
"""
import json
import requests

from .log import get_logger

log = get_logger("llm")

SYSTEM_PROMPT = """你是一个专业的视频生成 prompt 工程师。
用户会给你一段中文场景描述，请将其改写为高质量的英文视频生成 prompt。

要求：
1. 只输出英文 prompt，不要解释
2. 包含：主体描述、摄像机运动、光照氛围、画面风格
3. 控制在 1000 词以内
4. 适合 Seedance / Wan 等视频生成模型"""


def call_llm(config: dict, scene_desc: str, duration: int = 5) -> str:
    """
    调用 LLM API 优化 prompt

    Args:
        config: 全局配置（含 llm.api_url, llm.api_key, llm.model）
        scene_desc: 中文场景描述
        duration: 视频时长（秒）

    Returns:
        优化后的英文 prompt，失败返回空字符串
    """
    llm_cfg = config.get("llm", {})
    api_url = llm_cfg.get("api_url", "")
    api_key = llm_cfg.get("api_key", "")
    model = llm_cfg.get("model", "")
    max_tokens = llm_cfg.get("max_tokens", 1000)
    temperature = llm_cfg.get("temperature", 0.7)

    if not api_url or not api_key or api_key.startswith("sk-your"):
        log.warning("LLM API 未配置，跳过 prompt 优化")
        return ""

    user_msg = f"场景描述：{scene_desc}\n视频时长：{duration}秒"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        log.info(f"调用 LLM: {model} | {scene_desc[:30]}...")
        resp = requests.post(api_url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        result = data["choices"][0]["message"]["content"].strip()
        log.info(f"LLM 优化: {result[:60]}...")
        return result
    except Exception as e:
        log.error(f"LLM 调用失败: {e}")
        return ""