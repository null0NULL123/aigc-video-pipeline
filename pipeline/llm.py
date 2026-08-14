"""
LLM 客户端（OpenAI 兼容 /chat/completions）
用于：画面描述→英文视频 prompt、台词润色、屏幕字幕生成
失败或未配置时静默返回 None，由调用方走静态 fallback
"""
import aiohttp

from pipeline.log import get_logger

log = get_logger("llm")

SYSTEM_TRANSLATE = (
    "You are a professional prompt engineer for AI video generation (e.g. Seedance). "
    "Convert the user's scene description into a detailed, vivid English video prompt. "
    "Describe subject, action, environment, camera movement and lighting. "
    "Return ONLY the English prompt text, no explanation, no surrounding quotes."
)

SYSTEM_POLISH = (
    "You are a professional e-commerce copywriter. Polish the given product narration "
    "into punchy, persuasive, natural spoken copy suitable for short-video voiceover. "
    "Keep the same meaning and roughly the same length. Return ONLY the polished text."
)

SYSTEM_SCREEN_TEXT = (
    "You are a short-video subtitle expert. Generate one short attention-grabbing "
    "on-screen text (Chinese, no more than 15 characters) for the given scene and narration. "
    "Return ONLY the text, no punctuation at the end."
)


def is_enabled(config: dict) -> bool:
    """LLM 是否可用：开关开启 + api_url/api_key 已填且不是占位符"""
    llm_cfg = config.get("llm", {}) or {}
    if not llm_cfg.get("enabled", True):
        return False
    api_url = str(llm_cfg.get("api_url", "") or "").strip()
    api_key = str(llm_cfg.get("api_key", "") or "").strip()
    if not api_url or not api_key:
        return False
    if api_url.startswith("your-") or api_key.startswith("your-"):
        return False
    return True


async def chat(config: dict, system: str, user: str,
               max_tokens: int = 500, temperature: float = None) -> str | None:
    """调用 LLM，失败返回 None"""
    if not is_enabled(config):
        return None
    llm_cfg = config.get("llm", {}) or {}
    api_url = str(llm_cfg.get("api_url", "")).strip().rstrip("/")
    api_key = str(llm_cfg.get("api_key", "")).strip()
    if not api_url.endswith("/chat/completions"):
        api_url += "/chat/completions"

    payload = {
        "model": llm_cfg.get("model", "mimo-v2.5"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": int(llm_cfg.get("max_tokens", max_tokens) or max_tokens),
    }
    if temperature is not None:
        payload["temperature"] = temperature
    elif "temperature" in llm_cfg:
        payload["temperature"] = llm_cfg["temperature"]

    timeout = aiohttp.ClientTimeout(total=int(llm_cfg.get("timeout", 60)))
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(api_url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    log.warning(f"LLM HTTP {resp.status}: {body}")
                    return None
                data = await resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                return text.strip('"')
    except Exception as e:
        log.warning(f"LLM 调用失败: {e}")
        return None


async def translate_prompt(config: dict, scene_desc: str, dialogue: str, duration: int) -> str | None:
    """画面描述 → 英文视频生成 prompt"""
    user = (
        f"Scene description: {scene_desc}\n"
        f"Narration: {dialogue or '(none)'}\n"
        f"Video duration: {duration}s"
    )
    return await chat(config, SYSTEM_TRANSLATE, user, max_tokens=800)


async def polish_dialogue(config: dict, dialogue: str) -> str | None:
    """台词润色（电商口播风格）"""
    text = (dialogue or "").strip()
    if not text:
        return None
    return await chat(config, SYSTEM_POLISH, f"Narration: {text}", max_tokens=400)


async def suggest_screen_text(config: dict, scene_desc: str, dialogue: str) -> str | None:
    """根据场景+台词生成屏幕字幕"""
    user = f"Scene: {scene_desc or '(none)'}\nNarration: {dialogue or '(none)'}"
    return await chat(config, SYSTEM_SCREEN_TEXT, user, max_tokens=50)