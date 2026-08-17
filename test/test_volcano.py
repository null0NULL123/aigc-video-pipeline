"""
测试 pipeline/providers/volcano.py

策略：用 aiohttp.web 启动本地 mock server，验证：
- 请求体构造正确（model 短名→全名、参数透传、image→data URI）
- 响应解析正确
- 轮询直到 succeeded/failed
- 超时、重试、错误处理

不打真实 API。
依赖：pytest-asyncio（仅本测试文件需要）
"""
import asyncio
import base64
import json
from pathlib import Path
import pytest
import pytest_asyncio
import aiohttp
from aiohttp import web

from pipeline.providers.volcano import (
    VolcanoClient,
    VolcanoSeedream,
    VolcanoSeedance,
    _resolve_model_id,
    _file_to_data_uri,
    _to_image_url,
    MODEL_ALIASES,
)


# ──────────────── mock Ark server ────────────────

class FakeArk:
    """最小化的火山方舟 Ark API 模拟器"""

    def __init__(self):
        self.app = web.Application()
        self.app.router.add_post("/api/v3/images/generations", self._image_gen)
        self.app.router.add_post(
            "/api/v3/contents/generations/tasks", self._video_submit
        )
        self.app.router.add_get(
            "/api/v3/contents/generations/tasks/{tid}", self._video_status
        )
        self.last_image_body: dict | None = None
        self.last_video_body: dict | None = None
        self.image_response = {
            "model": "doubao-seedream-4-0-250828",
            "created": 1700000000,
            "data": [{"url": "https://example.com/img-001.png"}],
        }
        self.task_statuses: dict[str, list[str]] = {}

    async def _image_gen(self, request: web.Request) -> web.Response:
        self.last_image_body = await request.json()
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        return web.json_response(self.image_response)

    async def _video_submit(self, request: web.Request) -> web.Response:
        self.last_video_body = await request.json()
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        tid = "cgt-mock-12345"
        self.task_statuses[tid] = ["succeeded"]
        return web.json_response({"id": tid})

    async def _video_status(self, request: web.Request) -> web.Response:
        tid = request.match_info["tid"]
        seq = self.task_statuses.get(tid)
        if seq is None:
            # 未配置 → 默认成功
            return web.json_response({
                "id": tid,
                "status": "succeeded",
                "content": {"video_url": f"https://example.com/{tid}.mp4"},
            })
        # 按调用顺序返回状态；耗尽后保持最后一个
        if not hasattr(self, "_call_counts"):
            self._call_counts = {}
        idx = min(self._call_counts.get(tid, 0), len(seq) - 1)
        self._call_counts[tid] = idx + 1
        status = seq[idx]
        if status == "failed":
            return web.json_response({
                "id": tid,
                "status": "failed",
                "error": {"code": "InvalidParameter", "message": "test fail"},
            })
        if status == "succeeded":
            return web.json_response({
                "id": tid,
                "status": "succeeded",
                "content": {"video_url": f"https://example.com/{tid}.mp4"},
            })
        # queued / running
        return web.json_response({"id": tid, "status": status})


async def _start_ark_server(fake: FakeArk):
    runner = web.AppRunner(fake.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest_asyncio.fixture
async def ark_server():
    """启动 mock Ark server，返回 (fake_instance, base_url)

    base_url 已含 /api/v3 前缀（与 DEFAULT_BASE_URL 一致）
    """
    fake = FakeArk()
    runner, port_url = await _start_ark_server(fake)
    yield fake, f"{port_url}/api/v3"
    await runner.cleanup()


# ──────────────── 同步单测 ────────────────

class TestModelAliases:
    def test_short_names_resolved(self):
        """配置里的短名应该映射到完整 model ID"""
        assert _resolve_model_id("doubao-seedream-4.0") == "doubao-seedream-4-0-250828"
        assert _resolve_model_id("doubao-seedance-2-0-fast") == "doubao-seedance-2-0-fast-260128"
        assert _resolve_model_id("doubao-seedream-5.0") == "doubao-seedream-5-0-260128"

    def test_full_name_passthrough(self):
        """完整 model ID 不应被修改"""
        full = "doubao-seedream-4-0-250828"
        assert _resolve_model_id(full) == full

    def test_unknown_passthrough(self):
        """未知短名原样返回（让 Ark 报错而不是 hard-code 所有变体）"""
        assert _resolve_model_id("some-future-2099") == "some-future-2099"

    def test_aliases_cover_config(self):
        """至少覆盖 config.example.yaml 里出现的所有短名"""
        required = {
            "doubao-seedream-4.0",
            "doubao-seedream-4.5",
            "doubao-seedream-5.0",
            "doubao-seedance-2-0-fast",
            "doubao-seedance-2.0",
        }
        assert required.issubset(MODEL_ALIASES.keys()), \
            f"缺少别名: {required - MODEL_ALIASES.keys()}"


class TestDataURI:
    def test_jpeg_to_data_uri(self, tmp_path: Path):
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
        uri = _file_to_data_uri(img)
        assert uri.startswith("data:image/jpeg;base64,")
        assert "/9j/4GZha2UtanBlZw==" in uri

    def test_png_to_data_uri(self, tmp_path: Path):
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        uri = _file_to_data_uri(img)
        assert uri.startswith("data:image/png;base64,")

    def test_to_image_url_passthrough_http(self):
        url = "https://cdn.example.com/photo.jpg"
        assert _to_image_url(url) == url

    def test_to_image_url_passthrough_data(self):
        uri = "data:image/png;base64,iVBORw0KGgo="
        assert _to_image_url(uri) == uri

    def test_to_image_url_converts_local(self, tmp_path: Path):
        img = tmp_path / "local.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0x")
        result = _to_image_url(str(img))
        assert result.startswith("data:image/jpeg;base64,")

    def test_nonexistent_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            _file_to_data_uri(tmp_path / "nope.jpg")


class TestVolcanoClientConstructor:
    """构造函数单测（同步，无需 async）"""

    def test_api_key_empty_raises(self):
        with pytest.raises(ValueError):
            VolcanoClient(api_key="")

    def test_api_key_placeholder_raises(self):
        """your-xxx 占位符应被拒绝"""
        for bad in ["your-jimeng-api-key", "your-foo", "your-anything"]:
            with pytest.raises(ValueError):
                VolcanoClient(api_key=bad)


@pytest.mark.asyncio
class TestVolcanoClient:
    async def test_session_required_for_post(self):
        """不用 async with 直接调用应报错"""
        c = VolcanoClient(api_key="real-key")
        with pytest.raises(RuntimeError, match="async with"):
            await c._post("/x", {})

    @pytest.mark.asyncio
    async def test_post_constructs_request(self, ark_server):
        fake, base = ark_server
        async with VolcanoClient(api_key="real-key", base_url=base, timeout=10) as c:
            result = await c._post("/images/generations", {"prompt": "hi"})
        assert result["data"][0]["url"] == "https://example.com/img-001.png"

    @pytest.mark.asyncio
    async def test_error_response_raises(self):
        fake = FakeArk()
        async def fail(req):
            return web.json_response({"error": "bad key"}, status=401)
        fake.app.router.add_post("/fail", fail)
        runner, base = await _start_ark_server(fake)
        try:
            async with VolcanoClient(api_key="real-key", base_url=base) as c:
                with pytest.raises(RuntimeError, match="401"):
                    await c._post("/fail", {})
        finally:
            await runner.cleanup()


class TestVolcanoSeedream:
    @pytest.mark.asyncio
    async def test_generate_basic(self, ark_server):
        """基础文生图：短名应被解析，prompt/size 应透传"""
        fake, base = ark_server
        async with VolcanoClient(api_key="k", base_url=base) as c:
            seedream = VolcanoSeedream(c)
            result = await seedream.generate(
                prompt="a cat on a mat",
                model="doubao-seedream-4.0",
                size="1024x1024",
            )
        body = fake.last_image_body
        assert body["model"] == "doubao-seedream-4-0-250828"
        assert body["prompt"] == "a cat on a mat"
        assert body["size"] == "1024x1024"
        assert body["response_format"] == "url"
        assert body["watermark"] is False
        assert result["data"][0]["url"]

    @pytest.mark.asyncio
    async def test_generate_with_seed(self, ark_server):
        fake, base = ark_server
        async with VolcanoClient(api_key="k", base_url=base) as c:
            await VolcanoSeedream(c).generate(prompt="x", seed=42)
        assert fake.last_image_body["seed"] == 42

    @pytest.mark.asyncio
    async def test_seed_zero_omitted(self, ark_server):
        """seed=0 应被省略（让 API 随机），不传 seed 字段"""
        fake, base = ark_server
        async with VolcanoClient(api_key="k", base_url=base) as c:
            await VolcanoSeedream(c).generate(prompt="x", seed=0)
        assert "seed" not in fake.last_image_body

    @pytest.mark.asyncio
    async def test_local_image_to_data_uri(self, ark_server, tmp_path: Path):
        """本地参考图应转为 data URI"""
        fake, base = ark_server
        img = tmp_path / "ref.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nref")
        async with VolcanoClient(api_key="k", base_url=base) as c:
            await VolcanoSeedream(c).generate(
                prompt="edit this", image=[str(img)]
            )
        sent = fake.last_image_body["image"][0]
        assert sent.startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_remote_url_passthrough(self, ark_server):
        """http URL 应原样透传"""
        fake, base = ark_server
        async with VolcanoClient(api_key="k", base_url=base) as c:
            await VolcanoSeedream(c).generate(
                prompt="x",
                image=["https://cdn.example.com/photo.jpg"],
            )
        assert fake.last_image_body["image"] == [
            "https://cdn.example.com/photo.jpg"
        ]

    @pytest.mark.asyncio
    async def test_sequential_image_generation(self, ark_server):
        """max_images > 1 时应启用组图生成"""
        fake, base = ark_server
        async with VolcanoClient(api_key="k", base_url=base) as c:
            await VolcanoSeedream(c).generate(
                prompt="x", max_images=3,
                sequential_image_generation="auto",
            )
        body = fake.last_image_body
        assert body["sequential_image_generation"] == "auto"
        assert body["sequential_image_generation_options"]["max_images"] == 3

    @pytest.mark.asyncio
    async def test_empty_data_raises(self, ark_server):
        """data 为空时应报错"""
        fake, base = ark_server
        fake.image_response = {"data": []}
        async with VolcanoClient(api_key="k", base_url=base) as c:
            with pytest.raises(RuntimeError, match="空 data"):
                await VolcanoSeedream(c).generate(prompt="x")


class TestVolcanoSeedance:
    @pytest.mark.asyncio
    async def test_submit_basic(self, ark_server):
        """基础文生视频：短名解析、参数透传"""
        fake, base = ark_server
        async with VolcanoClient(api_key="k", base_url=base) as c:
            seedance = VolcanoSeedance(c)
            task_id = await seedance.submit(
                prompt="a cat walking",
                model="doubao-seedance-2-0-fast",
                duration=5,
                aspect_ratio="16:9",
                resolution="720p",
            )
        body = fake.last_video_body
        assert body["model"] == "doubao-seedance-2-0-fast-260128"
        assert body["ratio"] == "16:9"
        assert body["resolution"] == "720p"
        assert body["duration"] == 5
        assert body["generate_audio"] is True
        assert body["content"][0] == {
            "type": "text", "text": "a cat walking"
        }
        assert task_id == "cgt-mock-12345"

    @pytest.mark.asyncio
    async def test_first_frame(self, ark_server, tmp_path: Path):
        """首帧本地图片 → data URI，role=first_frame"""
        fake, base = ark_server
        img = tmp_path / "first.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0first")
        async with VolcanoClient(api_key="k", base_url=base) as c:
            await VolcanoSeedance(c).submit(
                prompt="animate", first_frame=str(img)
            )
        first = next(c for c in fake.last_video_body["content"]
                     if c.get("role") == "first_frame")
        assert first["type"] == "image_url"
        assert first["image_url"]["url"].startswith("data:image/jpeg;base64,")

    @pytest.mark.asyncio
    async def test_last_frame_and_refs(self, ark_server, tmp_path: Path):
        fake, base = ark_server
        first = tmp_path / "f.jpg"
        last = tmp_path / "l.jpg"
        ref = tmp_path / "r.jpg"
        for f in (first, last, ref):
            f.write_bytes(b"\xff\xd8\xff\xe0x")
        async with VolcanoClient(api_key="k", base_url=base) as c:
            await VolcanoSeedance(c).submit(
                prompt="x",
                first_frame=str(first),
                last_frame=str(last),
                ref_images=[str(ref)],
            )
        content = fake.last_video_body["content"]
        roles = [c.get("role") for c in content]
        assert "first_frame" in roles
        assert "last_frame" in roles
        assert "reference_image" in roles

    @pytest.mark.asyncio
    async def test_wait_for_completion_succeeded(self, ark_server):
        fake, base = ark_server
        async with VolcanoClient(
            api_key="k", base_url=base, poll_interval=0.01
        ) as c:
            result = await VolcanoSeedance(c).wait_for_completion(
                "cgt-mock-12345", max_wait=5
            )
        assert result["status"] == "succeeded"
        assert result["content"]["video_url"].endswith(".mp4")

    @pytest.mark.asyncio
    async def test_wait_failed_raises(self, ark_server):
        fake, base = ark_server
        fake.task_statuses["cgt-fail"] = ["failed"]
        async with VolcanoClient(
            api_key="k", base_url=base, poll_interval=0.01
        ) as c:
            with pytest.raises(RuntimeError, match="失败"):
                await VolcanoSeedance(c).wait_for_completion(
                    "cgt-fail", max_wait=5
                )

    @pytest.mark.asyncio
    async def test_wait_timeout_raises(self, ark_server):
        fake, base = ark_server
        fake.task_statuses["cgt-slow"] = ["queued", "running"]  # 永远不到 succeeded
        async with VolcanoClient(
            api_key="k", base_url=base, poll_interval=0.01
        ) as c:
            with pytest.raises(TimeoutError, match="超时"):
                await VolcanoSeedance(c).wait_for_completion(
                    "cgt-slow", max_wait=0.1
                )

    @pytest.mark.asyncio
    async def test_submit_no_task_id_raises(self, ark_server):
        """响应无 task_id 时应报错（mock _post 直接返回无 id）"""
        fake, base = ark_server
        async with VolcanoClient(api_key="k", base_url=base) as c:
            async def fake_post(path, body):
                return {"warning": "rate limit"}  # 200 OK, 但没有 id 字段
            c._post = fake_post
            seedance = VolcanoSeedance(c)
            with pytest.raises(RuntimeError, match="无 task_id"):
                await seedance.submit(prompt="x")


class TestVolcanoClientDownload:
    @pytest.mark.asyncio
    async def test_download_to_path(self, tmp_path: Path):
        """download() 把远程内容写到本地"""
        async def serve(req):
            return web.Response(body=b"fake-video-bytes")
        app = web.Application()
        app.router.add_get("/file.mp4", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with VolcanoClient(api_key="k") as c:
                dest = tmp_path / "out.mp4"
                await c.download(
                    f"http://127.0.0.1:{port}/file.mp4", dest
                )
            assert dest.exists()
            assert dest.read_bytes() == b"fake-video-bytes"
        finally:
            await runner.cleanup()