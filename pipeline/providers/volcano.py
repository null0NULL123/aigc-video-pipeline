"""
火山方舟 Ark API 客户端（异步）
直接 HTTP 调用，无需 ComfyUI 服务

提供：
- VolcanoClient    — 共享 HTTP 客户端（auth + session）
- VolcanoSeedream  — 文生图，对应 JimengSeedream4 节点
- VolcanoSeedance  — 文/图生视频（异步任务），对应 JimengSeedance2 节点

API 参考：
- 文生图: POST  {base}/v1/images/generations
- 视频任务: POST {base}/contents/generations/tasks
- 视频查询: GET  {base}/contents/generations/tasks/{task_id}
"""
import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from typing import Any
import aiohttp

from pipeline.log import get_logger
from pipeline.messages import Msg

log = get_logger("volcano")

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


# 短名（配置 / ComfyUI 节点习惯）→ 完整 API model ID
# 用户配置用短名是为了跟 JimengSeedream4 节点的 model_version 字段一致
MODEL_ALIASES: dict[str, str] = {
    # Seedream
    "doubao-seedream-3.0":       "doubao-seedream-3-0-t2i-250415",
    "doubao-seedream-4.0":       "doubao-seedream-4-0-250828",
    "doubao-seedream-4.5":       "doubao-seedream-4-5-251128",
    "doubao-seedream-5.0":       "doubao-seedream-5-0-260128",
    "doubao-seedream-5.0-lite":  "doubao-seedream-5-0-lite-260128",
    "doubao-seedream-5.0-pro":   "doubao-seedream-5-0-pro-260628",
    # Seedance
    "doubao-seedance-1.0-lite":  "doubao-seedance-1-0-lite-t2v-250428",
    "doubao-seedance-1.0-pro":   "doubao-seedance-1-0-pro-250528",
    "doubao-seedance-1.5-pro":   "doubao-seedance-1-5-pro-251215",
    "doubao-seedance-2-0":       "doubao-seedance-2-0-260128",
    "doubao-seedance-2.0":       "doubao-seedance-2-0-260128",
    "doubao-seedance-2-0-fast":  "doubao-seedance-2-0-fast-260128",
    "doubao-seedance-2.0-fast":  "doubao-seedance-2-0-fast-260128",
    "doubao-seedance-2-5":       "doubao-seedance-2-5-260628",
    "doubao-seedance-2.5":       "doubao-seedance-2-5-260628",
}


def _resolve_model_id(short_name: str) -> str:
    """短名 → 完整 API model ID；无别名则原样返回"""
    return MODEL_ALIASES.get(short_name, short_name)


def _file_to_data_uri(path: str | Path) -> str:
    """本地图片 → data:image/...;base64,...（Ark API 接受 data URI）"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"图片不存在: {path}")
    mime, _ = mimetypes.guess_type(str(p))
    mime = mime or "image/jpeg"
    b64 = base64.b64encode(p.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


def _to_image_url(path_or_url: str) -> str:
    """URL 原样；本地路径转 data URI"""
    if path_or_url.startswith(("http://", "https://", "data:")):
        return path_or_url
    return _file_to_data_uri(path_or_url)


class VolcanoClient:
    """火山方舟 Ark API 共享 HTTP 客户端"""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 timeout: int = 600, poll_interval: float = 5.0):
        if not api_key or api_key.startswith("your-"):
            raise ValueError("api_key 未配置或仍为占位符，请填写真实火山方舟 API key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.poll_interval = poll_interval
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc):
        if self.session:
            await self.session.close()

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _require_session(self) -> aiohttp.ClientSession:
        if not self.session:
            raise RuntimeError("VolcanoClient 必须用 async with 启动会话")
        return self.session

    async def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        log.info(f"POST {url}")
        session = self._require_session()
        async with session.post(url, json=body, headers=self._headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Ark API POST {path} {resp.status}: {text[:400]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Ark API 返回非 JSON: {text[:400]}") from e

    async def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        session = self._require_session()
        async with session.get(url, headers=self._headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Ark API GET {path} {resp.status}: {text[:400]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Ark API 返回非 JSON: {text[:400]}") from e

    async def download(self, url: str, dest: Path) -> Path:
        """下载远程资源（图 / 视频）到本地"""
        session = self._require_session()
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with session.get(url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                f.write(await resp.read())
        log.info(Msg.GEN_DOWNLOAD.format(path=str(dest)))
        return dest


class VolcanoSeedream:
    """对应 JimengSeedream4 节点 — 文生图（支持多参考图、组图）"""

    def __init__(self, client: VolcanoClient):
        self.client = client

    async def generate(
        self,
        *,
        prompt: str,
        model: str = "doubao-seedream-4.0",
        size: str = "2K",
        seed: int = 0,
        watermark: bool = False,
        image: list[str] | None = None,
        sequential_image_generation: str | None = None,
        max_images: int = 1,
        response_format: str = "url",
    ) -> dict:
        """
        调用 Ark 文生图接口，返回完整 JSON。
        返回 data[0].url 即生成的图片 URL（或 b64_json）。
        """
        body: dict[str, Any] = {
            "model": _resolve_model_id(model),
            "prompt": prompt,
            "size": size,
            "response_format": response_format,
            "watermark": watermark,
        }
        if image:
            body["image"] = [_to_image_url(u) for u in image]
        if seed:
            body["seed"] = seed
        if sequential_image_generation and max_images > 1:
            body["sequential_image_generation"] = sequential_image_generation
            body["sequential_image_generation_options"] = {"max_images": max_images}

        result = await self.client._post("/images/generations", body)
        n = len(result.get("data") or [])
        if n == 0:
            raise RuntimeError(f"Seedream 返回空 data: {result}")
        log.info(f"Seedream 已生成 {n} 张图")
        return result


class VolcanoSeedance:
    """对应 JimengSeedance2 节点 — 文/图生视频（异步任务）"""

    def __init__(self, client: VolcanoClient):
        self.client = client

    async def submit(
        self,
        *,
        prompt: str,
        model: str = "doubao-seedance-2-0-fast",
        first_frame: str = "",
        last_frame: str = "",
        ref_images: list[str] | None = None,
        ref_videos: list[str] | None = None,
        duration: int = 5,
        resolution: str = "720p",
        aspect_ratio: str = "16:9",
        generate_audio: bool = True,
        seed: int = 0,
        watermark: bool = False,
        filename_prefix: str = "seedance_output",
    ) -> str:
        """
        提交视频生成任务，返回 task_id。
        完成后用 wait_for_completion() 轮询取结果。
        """
        content: list[dict] = [{"type": "text", "text": prompt}]
        if first_frame:
            content.append({
                "type": "image_url",
                "image_url": {"url": _to_image_url(first_frame)},
                "role": "first_frame",
            })
        if last_frame:
            content.append({
                "type": "image_url",
                "image_url": {"url": _to_image_url(last_frame)},
                "role": "last_frame",
            })
        for img in ref_images or []:
            content.append({
                "type": "image_url",
                "image_url": {"url": _to_image_url(img)},
                "role": "reference_image",
            })
        for vid in ref_videos or []:
            content.append({
                "type": "video_url",
                "video_url": {"url": _to_image_url(vid)},
                "role": "reference_video",
            })

        body: dict[str, Any] = {
            "model": _resolve_model_id(model),
            "content": content,
            "resolution": resolution,
            "ratio": aspect_ratio,
            "duration": duration,
            "generate_audio": generate_audio,
            "watermark": watermark,
        }
        if seed:
            body["seed"] = seed

        result = await self.client._post("/contents/generations/tasks", body)
        task_id = result.get("id")
        if not task_id:
            raise RuntimeError(f"Seedance 提交无 task_id: {result}")
        log.info(Msg.GEN_SUBMIT.format(pid=task_id))
        return task_id

    async def wait_for_completion(
        self, task_id: str, max_wait: int = 600
    ) -> dict:
        """
        轮询直到 succeeded / failed。
        返回完整任务结果（含 content.video_url）。
        """
        start = asyncio.get_event_loop().time()
        last_status = ""
        while True:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > max_wait:
                raise TimeoutError(
                    f"Seedance 任务超时 ({max_wait}s): {task_id}"
                )
            result = await self.client._get(
                f"/contents/generations/tasks/{task_id}"
            )
            status = result.get("status", "queued")
            if status != last_status:
                log.info(Msg.GEN_STATUS.format(
                    pid=task_id[:24], status=status
                ))
                last_status = status
            if status == "succeeded":
                log.info(Msg.GEN_DONE.format(
                    pid=task_id, elapsed=f"{elapsed:.1f}"
                ))
                return result
            if status == "failed":
                err = result.get("error") or {}
                msg = err.get("message", "unknown")
                raise RuntimeError(
                    f"Seedance 任务失败 {task_id}: {msg}"
                )
            await asyncio.sleep(self.client.poll_interval)