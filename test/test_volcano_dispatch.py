"""
测试 pipeline/generator.py 中的火山方舟 dispatch 逻辑

覆盖：
- _is_volcano_eligible：节点判定
- _has_volcano_key：api_key 校验
- _submit() 入口 dispatch：Jimeng 工作流走火山，其他走 ComfyUI
- _submit_volcano：单 Seedream / 单 Seedance / 链式 Seedream→Seedance
- _wait_volcano：轮询 + 下载
"""
import asyncio
import base64
import json
import tempfile
from pathlib import Path
import pytest
import pytest_asyncio
import aiohttp
from aiohttp import web

from pipeline.generator import (
    _is_volcano_eligible,
    _has_volcano_key,
    _submit,
    _submit_volcano,
    _wait_volcano,
)


# ──────────────── 单元测试：纯逻辑 ────────────────

class TestIsVolcanoEligible:
    """工作流是否可走火山方舟直连（只含 Jimeng + 文件 I/O glue）"""

    def test_seedream_only_eligible(self):
        wf = {
            "1": {"class_type": "JimengSeedream4", "inputs": {}},
            "2": {"class_type": "JimengAPIClient", "inputs": {}},
            "3": {"class_type": "SaveImage", "inputs": {}},
        }
        assert _is_volcano_eligible(wf) is True

    def test_seedance_only_eligible(self):
        wf = {
            "1": {"class_type": "JimengSeedance2", "inputs": {}},
            "2": {"class_type": "JimengAPIClient", "inputs": {}},
            "3": {"class_type": "SaveVideo", "inputs": {}},
            "4": {"class_type": "LoadImage", "inputs": {}},
        }
        assert _is_volcano_eligible(wf) is True

    def test_chain_eligible(self):
        wf = {
            "1": {"class_type": "JimengSeedream4", "inputs": {}},
            "2": {"class_type": "JimengSeedance2", "inputs": {}},
            "3": {"class_type": "SaveVideo", "inputs": {}},
        }
        assert _is_volcano_eligible(wf) is True

    def test_unknown_node_blocks(self):
        """KSampler/CLIPTextEncode 等本地推理节点 → 不能走火山"""
        wf = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {}},
            "2": {"class_type": "KSampler", "inputs": {}},
        }
        assert _is_volcano_eligible(wf) is False

    def test_jimeng_plus_unknown_blocks(self):
        wf = {
            "1": {"class_type": "JimengSeedream4", "inputs": {}},
            "2": {"class_type": "KSampler", "inputs": {}},  # 混入未知节点
        }
        assert _is_volcano_eligible(wf) is False

    def test_empty_wf_not_eligible(self):
        assert _is_volcano_eligible({}) is False

    def test_none_wf_not_eligible(self):
        assert _is_volcano_eligible(None) is False

    def test_non_dict_node_handled(self):
        wf = {
            "1": {"class_type": "JimengSeedream4", "inputs": {}},
            "2": None,  # 异常节点不阻断
        }
        assert _is_volcano_eligible(wf) is True


class TestHasVolcanoKey:
    """config.api.jimeng.api_key 校验"""

    def test_real_key_passes(self):
        assert _has_volcano_key({"api": {"jimeng": {"api_key": "real-key-xxx"}}}) is True

    def test_placeholder_rejected(self):
        cfg = {"api": {"jimeng": {"api_key": "your-jimeng-api-key"}}}
        assert _has_volcano_key(cfg) is False

    def test_empty_rejected(self):
        assert _has_volcano_key({"api": {"jimeng": {"api_key": ""}}}) is False

    def test_missing_api_rejected(self):
        assert _has_volcano_key({}) is False

    def test_missing_jimeng_rejected(self):
        assert _has_volcano_key({"api": {}}) is False


# ──────────────── mock Ark server（dispatch 集成测试） ────────────────

class _FakeArkForDispatch:
    """dispatch 测试用的最小 Ark mock"""

    # 提供一张 1x1 PNG（最小有效 PNG）
    PNG_BYTES = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63f8cf00000003000100ad3f2c2e0000000049454e44"
        "ae426082"
    )

    def __init__(self):
        self.app = web.Application()
        self.app.router.add_post("/api/v3/images/generations", self._image)
        self.app.router.add_post("/api/v3/contents/generations/tasks", self._task)
        self.app.router.add_get(
            "/api/v3/contents/generations/tasks/{tid}", self._status
        )
        # 同时充当图床 / 视频床（让 _download_image_result 和 _wait_volcano 能拿到）
        self.app.router.add_get("/img.png", self._serve_image)
        self.app.router.add_get("/result.mp4", self._serve_video)
        self.last_image_body = None
        self.last_task_body = None
        self.image_count = 0
        # 启动时填充
        self.image_url = ""
        self.video_url = ""

    def substitute_host(self, base_url: str):
        """在 server 启动后调用，更新响应 URL 指向本 mock server 的根路径"""
        host = base_url.replace("/api/v3", "")
        self.image_url = f"{host}/img.png"
        self.video_url = f"{host}/result.mp4"

    async def _image(self, req: web.Request) -> web.Response:
        self.last_image_body = await req.json()
        self.image_count += 1
        return web.json_response({
            "model": "doubao-seedream-4-0-250828",
            "data": [{"url": self.image_url}],
        })

    async def _task(self, req: web.Request) -> web.Response:
        self.last_task_body = await req.json()
        return web.json_response({"id": "cgt-dispatch-test"})

    async def _status(self, req: web.Request) -> web.Response:
        return web.json_response({
            "id": req.match_info["tid"],
            "status": "succeeded",
            "content": {"video_url": self.video_url},
        })

    async def _serve_image(self, req: web.Request) -> web.Response:
        return web.Response(body=self.PNG_BYTES, content_type="image/png")

    async def _serve_video(self, req: web.Request) -> web.Response:
        return web.Response(body=b"fake-mp4-bytes", content_type="video/mp4")


async def _start_fake_ark(fake):
    runner = web.AppRunner(fake.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/api/v3"


@pytest_asyncio.fixture
async def fake_ark():
    fake = _FakeArkForDispatch()
    runner, base = await _start_fake_ark(fake)
    fake.substitute_host(base)
    yield fake, base
    await runner.cleanup()


def _make_volcano_client(base_url: str):
    """构造一个真正可用的 VolcanoClient（指向 mock server）"""
    from pipeline.providers.volcano import VolcanoClient
    return VolcanoClient(api_key="test-key-real", base_url=base_url)


# ──────────────── _submit() 入口 dispatch 测试 ────────────────

@pytest.mark.asyncio
class TestSubmitDispatch:
    async def test_jimeng_workflow_goes_volcano(self, fake_ark):
        """Jimeng 工作流 + api_key + volcano_client → 走火山"""
        fake, base = fake_ark
        vc = _make_volcano_client(base)
        async with vc:
            wf = {"workflow": {
                "1": {"class_type": "JimengSeedream4", "inputs": {}},
                "2": {"class_type": "SaveImage", "inputs": {}},
            }}
            state = {
                "id": "1",
                "workflow_data": wf,
                "workflow_type": "comfyui",
                "optimized_prompt": "a cat",
                "duration": 5,
                "config": {"api": {"jimeng": {"api_key": "real-key"}}},
                "_ctx": {"volcano_client": vc},
            }
            result = await _submit(state)
        assert result["status"] == "done"
        assert result["_provider"] == "volcano"
        assert result["video_path"].endswith(".png")
        assert fake.image_count == 1

    async def test_no_volcano_client_falls_back_to_comfyui(self, fake_ark):
        """没 volcano_client → 走原 ComfyUI 路径（但没有真 ComfyUI 会失败）"""
        fake, base = fake_ark
        wf = {"workflow": {
            "1": {"class_type": "JimengSeedream4", "inputs": {}},
        }}
        # 用一个 fake comfy client 让 _submit 不爆错
        class _FakeComfy:
            async def submit(self, wf): return "fake-pid"
        state = {
            "id": "1",
            "workflow_data": wf,
            "workflow_type": "comfyui",
            "optimized_prompt": "x",
            "duration": 5,
            "config": {"api": {"jimeng": {"api_key": "real-key"}}},
            "_ctx": {"client": _FakeComfy()},  # 没有 volcano_client
        }
        result = await _submit(state)
        # 走了 ComfyUI 路径（不报错，因为 _FakeComfy 不抛）
        assert result.get("prompt_id") == "fake-pid"

    async def test_placeholder_key_falls_back_to_comfyui(self, fake_ark):
        """api_key 是占位符 → 走 ComfyUI"""
        fake, base = fake_ark
        vc = _make_volcano_client(base)
        class _FakeComfy:
            async def submit(self, wf): return "fake-pid"
        wf = {"workflow": {
            "1": {"class_type": "JimengSeedream4", "inputs": {}},
        }}
        state = {
            "id": "1",
            "workflow_data": wf,
            "workflow_type": "comfyui",
            "optimized_prompt": "x",
            "duration": 5,
            "config": {"api": {"jimeng": {"api_key": "your-jimeng-api-key"}}},
            "_ctx": {"client": _FakeComfy(), "volcano_client": vc},
        }
        result = await _submit(state)
        # 占位符 → 火山被跳过，走 ComfyUI
        assert result.get("prompt_id") == "fake-pid"

    async def test_non_jimeng_workflow_skips_volcano(self, fake_ark):
        """非 Jimeng 工作流 → 走 ComfyUI"""
        fake, base = fake_ark
        vc = _make_volcano_client(base)
        class _FakeComfy:
            async def submit(self, wf): return "fake-pid"
        wf = {"workflow": {
            "1": {"class_type": "CLIPTextEncode", "inputs": {}},
            "2": {"class_type": "KSampler", "inputs": {}},
        }}
        state = {
            "id": "1",
            "workflow_data": wf,
            "workflow_type": "comfyui",
            "optimized_prompt": "x",
            "duration": 5,
            "config": {"api": {"jimeng": {"api_key": "real-key"}}},
            "_ctx": {"client": _FakeComfy(), "volcano_client": vc},
        }
        result = await _submit(state)
        # 走 ComfyUI
        assert result.get("prompt_id") == "fake-pid"


# ──────────────── _submit_volcano() 详细逻辑 ────────────────

@pytest.mark.asyncio
class TestSubmitVolcano:

    async def test_single_seedream_returns_done(self, fake_ark):
        """单 Seedream：返回 status=done，video_path=本地 PNG"""
        fake, base = fake_ark
        vc = _make_volcano_client(base)
        async with vc:
            wf = {"workflow": {
                "1": {"class_type": "JimengSeedream4", "inputs": {"prompt": ""}},
                "2": {"class_type": "SaveImage", "inputs": {}},
            }}
            state = {
                "id": "5",
                "optimized_prompt": "a cat on a mat",
                "duration": 5,
                "config": {
                    "_batch_id": "b1",
                    "api": {"jimeng": {"api_key": "real-key"}},
                    "output": {"shots_dir": "output/test_shots"},
                    "seedream": {"model_version": "doubao-seedream-4.0", "size": "1024x1024"},
                    "agent": {"default_seed": 42},
                },
                "workflow_data": wf,
                "_ctx": {"volcano_client": vc},
            }
            result = await _submit_volcano(state)
        assert result["status"] == "done"
        assert result["_provider"] == "volcano"
        assert result["video_path"].endswith("b1_shot_5.png")
        # request body 检查
        assert fake.last_image_body["prompt"] == "a cat on a mat"
        assert fake.last_image_body["model"] == "doubao-seedream-4-0-250828"
        assert fake.last_image_body["size"] == "1024x1024"
        assert fake.last_image_body["seed"] == 42

    async def test_single_seedance_returns_submitted(self, fake_ark):
        """单 Seedance：返回 status=submitted, task_id"""
        fake, base = fake_ark
        vc = _make_volcano_client(base)
        async with vc:
            wf = {"workflow": {
                "1": {"class_type": "JimengSeedance2", "inputs": {"prompt": ""}},
                "2": {"class_type": "SaveVideo", "inputs": {}},
            }}
            state = {
                "id": "3",
                "optimized_prompt": "sunset",
                "duration": 7,
                "config": {
                    "_batch_id": "b2",
                    "api": {"jimeng": {"api_key": "real-key"}},
                    "output": {"shots_dir": "output/test_shots"},
                    "seedance": {"model_version": "doubao-seedance-2-0-fast"},
                    "agent": {"default_seed": 0},
                },
                "workflow_data": wf,
                "_ctx": {"volcano_client": vc},
            }
            result = await _submit_volcano(state)
        assert result["status"] == "submitted"
        assert result["_provider"] == "volcano"
        assert result["task_id"] == "cgt-dispatch-test"
        assert fake.last_task_body["model"] == "doubao-seedance-2-0-fast-260128"
        assert fake.last_task_body["duration"] == 7
        # 链式判定：无 Seedream → motion_prompt 不该被使用
        assert fake.last_task_body["content"][0]["text"] == "sunset"

    async def test_chain_seedream_then_seedance(self, fake_ark):
        """链式：先 Seedream，再 Seedance 用其结果作 first_frame"""
        fake, base = fake_ark
        vc = _make_volcano_client(base)
        async with vc:
            wf = {"workflow": {
                "1": {"class_type": "JimengSeedream4", "inputs": {}},
                "2": {"class_type": "JimengSeedance2", "inputs": {}},
                "3": {"class_type": "SaveVideo", "inputs": {}},
            }}
            state = {
                "id": "1",
                "optimized_prompt": "still scene",
                "motion_prompt": "slow motion, camera push-in",
                "duration": 5,
                "config": {
                    "_batch_id": "b3",
                    "api": {"jimeng": {"api_key": "real-key"}},
                    "output": {"shots_dir": "output/test_shots"},
                    "seedream": {"model_version": "doubao-seedream-4.0"},
                    "seedance": {"model_version": "doubao-seedance-2-0-fast"},
                    "agent": {"default_seed": 0},
                },
                "workflow_data": wf,
                "_ctx": {"volcano_client": vc},
            }
            result = await _submit_volcano(state)
        assert result["status"] == "submitted"
        # Seedream 被调用 1 次
        assert fake.image_count == 1
        # Seedance 用了 motion_prompt
        assert fake.last_task_body["content"][0]["text"] == "slow motion, camera push-in"
        # Seedance 的 first_frame 是 Seedream 返回的 URL（指向 mock 图床）
        first_frame_item = next(c for c in fake.last_task_body["content"]
                                if c.get("role") == "first_frame")
        assert first_frame_item["image_url"]["url"].startswith("http")
        assert first_frame_item["image_url"]["url"].endswith("/img.png")

    async def test_local_first_frame_to_data_uri(self, fake_ark, tmp_path):
        """本地首帧 → 转 data URI"""
        fake, base = fake_ark
        vc = _make_volcano_client(base)
        async with vc:
            f = tmp_path / "first.jpg"
            f.write_bytes(b"\xff\xd8\xff\xe0test")
            wf = {"workflow": {
                "1": {"class_type": "JimengSeedance2", "inputs": {}},
            }}
            state = {
                "id": "9",
                "optimized_prompt": "x",
                "duration": 5,
                "first_frame": str(f),
                "config": {
                    "api": {"jimeng": {"api_key": "real-key"}},
                    "output": {"shots_dir": "output/test_shots"},
                    "seedance": {"model_version": "doubao-seedance-2-0-fast"},
                    "agent": {"default_seed": 0},
                },
                "workflow_data": wf,
                "_ctx": {"volcano_client": vc},
            }
            result = await _submit_volcano(state)
        assert result["status"] == "submitted"
        ff = next(c for c in fake.last_task_body["content"]
                  if c.get("role") == "first_frame")
        assert ff["image_url"]["url"].startswith("data:image/jpeg;base64,")

    async def test_no_volcano_client_fails(self):
        """无 volcano_client → failed"""
        state = {
            "id": "1",
            "workflow_data": {"workflow": {
                "1": {"class_type": "JimengSeedream4", "inputs": {}},
            }},
            "_ctx": {},
        }
        result = await _submit_volcano(state)
        assert result["status"] == "failed"
        assert "无 VolcanoClient" in result["error_message"]

    async def test_no_jimeng_nodes_fails(self, fake_ark):
        """工作流无 Jimeng 节点 → failed"""
        fake, base = fake_ark
        vc = _make_volcano_client(base)
        async with vc:
            wf = {"workflow": {
                "1": {"class_type": "KSampler", "inputs": {}},
            }}
            state = {
                "id": "1",
                "workflow_data": wf,
                "_ctx": {"volcano_client": vc},
            }
            result = await _submit_volcano(state)
        assert result["status"] == "failed"
        assert "无 Jimeng 节点" in result["error_message"]


# ──────────────── _wait_volcano() ────────────────

@pytest.mark.asyncio
class TestWaitVolcano:

    async def test_succeeds_and_downloads(self, fake_ark, tmp_path):
        fake, base = fake_ark
        vc = _make_volcano_client(base)
        async with vc:
            state = {
                "id": "1",
                "task_id": "cgt-dispatch-test",
                "config": {
                    "_batch_id": "b1",
                    "output": {"shots_dir": str(tmp_path)},
                },
                "_ctx": {"volcano_client": vc},
            }
            result = await _wait_volcano(state)
        assert result["status"] == "done"
        assert result["video_path"].endswith("b1_shot_1.mp4")
        assert Path(result["video_path"]).exists()

    async def test_no_task_id_fails(self, fake_ark):
        fake, base = fake_ark
        vc = _make_volcano_client(base)
        async with vc:
            state = {
                "id": "1",
                "config": {"_batch_id": "b", "output": {"shots_dir": "/tmp"}},
                "_ctx": {"volcano_client": vc},
            }
            result = await _wait_volcano(state)
        assert result["status"] == "failed"

    async def test_no_client_fails(self):
        state = {
            "id": "1",
            "task_id": "cgt-x",
            "config": {},
            "_ctx": {},
        }
        result = await _wait_volcano(state)
        assert result["status"] == "failed"