"""生成链路测试：LLM 开启/关闭/失败时的 prompt 与回退"""
import json
import tempfile
from pathlib import Path

import pytest

from pipeline.generator import _polish, _optimize_prompt, _select

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CONFIG = {
    "agent": {
        "camera": "smooth slow camera push-in",
        "style": "cinematic lighting",
        "default_duration": 5,
        "prompt_long_duration": 8,
        "prompt_short_duration": 4,
    },
    "llm": {"enabled": False, "api_url": "", "api_key": "", "model": "mimo-v2.5"},
}


def _state(**over):
    s = {
        "id": "1",
        "scene_desc": "产品特写，金属质感",
        "dialogue": "这是一款超薄键盘",
        "screen_text": "",
        "duration": 5,
        "key_elements": [],
        "config": CONFIG,
    }
    s.update(over)
    return s


def _registry():
    from pipeline.registry import TemplateRegistry
    return TemplateRegistry(str(PROJECT_ROOT / "templates"), config={})


@pytest.mark.asyncio
async def test_optimize_fallback_static():
    """LLM 未配置 → 静态拼接 prompt"""
    state = await _optimize_prompt(_state())
    assert state["optimized_prompt"].startswith("professional technology scene")
    assert "smooth slow camera push-in" in state["optimized_prompt"]


@pytest.mark.asyncio
async def test_optimize_duration_suffix_long():
    state = await _optimize_prompt(_state(duration=12))
    assert "slow paced" in state["optimized_prompt"]


@pytest.mark.asyncio
async def test_optimize_duration_suffix_short():
    state = await _optimize_prompt(_state(duration=2))
    assert "quick dynamic" in state["optimized_prompt"]


@pytest.mark.asyncio
async def test_optimize_with_llm(monkeypatch):
    """LLM 开启 → 使用 LLM 翻译结果"""
    from pipeline import llm as llm_client

    async def fake_translate(config, scene_desc, dialogue, duration):
        return "a shiny metallic keyboard close-up"

    monkeypatch.setattr(llm_client, "is_enabled", lambda config: True)
    monkeypatch.setattr(llm_client, "translate_prompt", fake_translate)

    cfg = dict(CONFIG)
    cfg["llm"] = {"enabled": True, "api_url": "http://x/v1", "api_key": "k"}
    state = await _optimize_prompt(_state(config=cfg))
    assert state["optimized_prompt"] == "a shiny metallic keyboard close-up"


# ---------- _select 工作流选择 ----------

def test_select_auto_default():
    """workflow_id 为空 → 自动匹配"""
    reg = _registry()
    state = _select(_state(_ctx={"registry": reg}))
    assert state["workflow_id"] == "seedance_t2v"


def test_select_explicit_workflow():
    """显式指定工作流 → 直接采用"""
    reg = _registry()
    state = _select(_state(workflow_id="seedream_t2i", _ctx={"registry": reg}))
    assert state["workflow_id"] == "seedream_t2i"
    assert state["workflow_type"] == "comfyui"


def test_select_explicit_i2v_with_image():
    """图生视频：素材存在时用显式 i2v"""
    reg = _registry()
    state = _select(_state(
        asset_type="image", asset_path=str(PROJECT_ROOT / "templates"),
        workflow_id="seedance_i2v", _ctx={"registry": reg},
    ))
    assert state["workflow_id"] == "seedance_i2v"


def test_select_explicit_missing_falls_back():
    """显式工作流不存在 → 回退自动匹配"""
    reg = _registry()
    state = _select(_state(workflow_id="no_such_workflow", _ctx={"registry": reg}))
    assert state["workflow_id"] == "seedance_t2v"


def test_select_image_asset_derives_i2v():
    """assets 含图片 → 推导 image 类型 → i2v"""
    reg = _registry()
    state = _select(_state(
        assets=[{"type": "image", "path": str(PROJECT_ROOT / "templates")}],
        _ctx={"registry": reg},
    ))
    assert state["workflow_id"] == "seedance_i2v"


def test_select_first_frame_derives_image():
    """显式首帧 → image 类型"""
    reg = _registry()
    state = _select(_state(first_frame=str(PROJECT_ROOT / "templates"), _ctx={"registry": reg}))
    assert state["workflow_id"] == "seedance_i2v"


def test_select_text_asset_stays_t2v():
    """仅文本素材 → ai_generated → t2v"""
    reg = _registry()
    state = _select(_state(assets=[{"type": "text", "content": "标题"}], _ctx={"registry": reg}))
    assert state["workflow_id"] == "seedance_t2v"


@pytest.mark.asyncio
async def test_optimize_llm_failure_fallback(monkeypatch):
    """LLM 调用失败 → 回退静态"""
    from pipeline import llm as llm_client

    async def fake_translate(config, scene_desc, dialogue, duration):
        return None

    monkeypatch.setattr(llm_client, "is_enabled", lambda config: True)
    monkeypatch.setattr(llm_client, "translate_prompt", fake_translate)

    cfg = dict(CONFIG)
    cfg["llm"] = {"enabled": True, "api_url": "http://x/v1", "api_key": "k"}
    state = await _optimize_prompt(_state(config=cfg))
    assert state["optimized_prompt"].startswith("professional technology scene")


@pytest.mark.asyncio
async def test_polish_disabled_keeps_values():
    """LLM 未启用 → 台词/字幕保持原值"""
    state = await _polish(_state())
    assert state["dialogue"] == "这是一款超薄键盘"
    assert state["screen_text"] == ""


@pytest.mark.asyncio
async def test_polish_enabled(monkeypatch):
    """LLM 启用 → 台词润色 + 空字幕补全"""
    from pipeline import llm as llm_client

    async def fake_polish(config, dialogue):
        return "【润色】这是一款超薄键盘"

    async def fake_suggest(config, scene_desc, dialogue):
        return "超薄键盘"

    monkeypatch.setattr(llm_client, "is_enabled", lambda config: True)
    monkeypatch.setattr(llm_client, "polish_dialogue", fake_polish)
    monkeypatch.setattr(llm_client, "suggest_screen_text", fake_suggest)

    cfg = dict(CONFIG)
    cfg["llm"] = {"enabled": True, "api_url": "http://x/v1", "api_key": "k"}
    state = await _polish(_state(config=cfg))
    assert state["dialogue"] == "【润色】这是一款超薄键盘"
    assert state["screen_text"] == "超薄键盘"


@pytest.mark.asyncio
async def test_polish_keeps_existing_screen_text(monkeypatch):
    """已有屏幕字幕时不覆盖"""
    from pipeline import llm as llm_client

    monkeypatch.setattr(llm_client, "is_enabled", lambda config: True)

    async def fake_polish(config, dialogue):
        return "润色后"

    monkeypatch.setattr(llm_client, "polish_dialogue", fake_polish)

    cfg = dict(CONFIG)
    cfg["llm"] = {"enabled": True, "api_url": "http://x/v1", "api_key": "k"}
    state = await _polish(_state(config=cfg, screen_text="已有字幕"))
    assert state["dialogue"] == "润色后"
    assert state["screen_text"] == "已有字幕"


# ---------- Seedream 链式工作流 ----------

def _chain_workflow():
    """Seedream4 → Seedance2 → SaveVideo 链式工作流"""
    return {
        "workflow": {
            "1": {"class_type": "JimengSeedream4", "inputs": {"prompt": "", "seed": 0, "images": {}}},
            "2": {"class_type": "JimengAPIClient", "inputs": {}},
            "3": {"class_type": "JimengSeedance2", "inputs": {"prompt": "", "duration": 5, "seed": 0,
                                                             "filename_prefix": "", "first_frame_image": ["1", 0]}},
            "4": {"class_type": "SaveVideo", "inputs": {"filename_prefix": ""}},
        }
    }


def _t2i_workflow():
    """独立 T2I：Seedream4 → SaveImage"""
    return {
        "workflow": {
            "1": {"class_type": "JimengSeedream4", "inputs": {"prompt": "", "seed": 0, "images": {}}},
            "2": {"class_type": "JimengAPIClient", "inputs": {}},
            "3": {"class_type": "SaveImage", "inputs": {"filename_prefix": ""}},
        }
    }


@pytest.mark.asyncio
async def test_optimize_chain_generates_motion_prompt():
    """链式工作流 → 额外生成 motion_prompt"""
    state = await _optimize_prompt(_state(workflow_data=_chain_workflow()))
    assert "optimized_prompt" in state
    assert "motion_prompt" in state
    assert "camera" in state["motion_prompt"]


@pytest.mark.asyncio
async def test_optimize_non_chain_no_motion_prompt():
    """普通 T2V → 不生成 motion_prompt"""
    state = await _optimize_prompt(_state(workflow_data={"workflow": {"3": {"class_type": "JimengSeedance2", "inputs": {}}}}))
    assert "motion_prompt" not in state


@pytest.mark.asyncio
async def test_generate_shot_passes_asset_model_fields():
    """generate_shot 必须把 assets/首尾帧/workflow_id 透传给 _select/_submit"""
    from pipeline.generator import generate_shot
    d = Path(tempfile.mkdtemp())
    f1, f2 = d / "x.png", d / "y.png"
    f1.write_bytes(b"x")
    f2.write_bytes(b"y")

    fake = _FakeClient()
    async def _wait(pid):
        return {"outputs": {}}
    fake.wait_for_completion = _wait
    async def _download(history, out_dir, sid):
        return "x.mp4"
    fake.download_output = _download
    reg = _registry()
    calls = []

    import pipeline.generator as gen
    orig_submit = gen._submit

    async def spy_submit(state):
        calls.append({
            "assets": state.get("assets"),
            "first_frame": state.get("first_frame"),
            "last_frame": state.get("last_frame"),
            "workflow_id": state.get("workflow_id"),
            "asset_type": state.get("asset_type"),
        })
        return await orig_submit(state)

    gen._submit = spy_submit
    try:
        shot = {
            "id": "1", "scene_desc": "test", "duration": 5,
            "asset_type": "ai_generated", "assets": [{"type": "image", "path": str(f1)}],
            "first_frame": str(f1), "last_frame": str(f2),
            "workflow_id": "seedance_i2v",
        }
        await generate_shot(shot, dict(CONFIG), reg, fake, {"registry": reg, "client": fake})
    finally:
        gen._submit = orig_submit

    assert calls, "should reach submit"
    c = calls[0]
    assert c["assets"] == [{"type": "image", "path": str(f1)}]
    assert c["first_frame"] == str(f1)
    assert c["last_frame"] == str(f2)
    assert c["workflow_id"] == "seedance_i2v"
    assert c["asset_type"] == "image"


class _FakeClient:
    def __init__(self):
        self.submitted = None

    async def submit(self, workflow):
        self.submitted = workflow
        return "pid-123"

    async def upload_image(self, path):
        return "server_file"


@pytest.mark.asyncio
async def test_submit_chain_dual_prompt():
    """链式提交：Seedream 用画面 prompt，Seedance 用 motion prompt"""
    from pipeline.generator import _submit
    fake = _FakeClient()
    cfg = dict(CONFIG)
    cfg["_batch_id"] = "batch_demo"
    state = _state(
        config=cfg,
        workflow_data=_chain_workflow(),
        workflow_type="comfyui",
        _ctx={"client": fake},
        optimized_prompt="a shiny metallic keyboard",
        motion_prompt="slow camera push-in, subtle motion",
        duration=5,
    )
    result = await _submit(state)
    assert result["status"] == "submitted"
    wf = fake.submitted
    assert wf["1"]["inputs"]["prompt"] == "a shiny metallic keyboard"
    assert wf["3"]["inputs"]["prompt"] == "slow camera push-in, subtle motion"
    assert wf["3"]["inputs"]["filename_prefix"] == "batch_demo_shot_1"
    assert wf["4"]["inputs"]["filename_prefix"] == "batch_demo_shot_1"


@pytest.mark.asyncio
async def test_submit_t2i_sets_saveimage_prefix():
    """T2I 提交：prompt 注入 Seedream，SaveImage 前缀设置"""
    from pipeline.generator import _submit
    fake = _FakeClient()
    cfg = dict(CONFIG)
    cfg["_batch_id"] = "batch_demo"
    state = _state(
        config=cfg,
        workflow_data=_t2i_workflow(),
        workflow_type="comfyui",
        _ctx={"client": fake},
        optimized_prompt="a tech product poster",
        duration=5,
    )
    result = await _submit(state)
    assert result["status"] == "submitted"
    wf = fake.submitted
    assert wf["1"]["inputs"]["prompt"] == "a tech product poster"
    assert wf["3"]["inputs"]["filename_prefix"] == "batch_demo_shot_1"


class _RefClient(_FakeClient):
    def __init__(self):
        super().__init__()
        self.uploads = []

    async def upload_image(self, path):
        self.uploads.append(path)
        return "srv_" + Path(path).name

    async def upload_video(self, path):
        self.uploads.append(path)
        return "srv_" + Path(path).name


@pytest.mark.asyncio
async def test_submit_first_last_frame_and_video_ref():
    """首帧/尾帧/视频参考注入：LoadImage + 动态尾帧节点 + ref_videos"""
    from pipeline.generator import _submit
    tpl = json.loads((PROJECT_ROOT / "templates" / "seedance_i2v.json").read_text(encoding="utf-8"))
    d = Path(tempfile.mkdtemp())
    f1, f2, v1 = d / "first.png", d / "last.png", d / "ref.mp4"
    for f in (f1, f2, v1):
        f.write_bytes(b"x")

    fake = _RefClient()
    state = _state(
        workflow_data=tpl, workflow_type="comfyui", _ctx={"client": fake},
        optimized_prompt="a sunset over the sea", duration=5,
        first_frame=str(f1), last_frame=str(f2),
        assets=[{"type": "video", "path": str(v1)}],
    )
    result = await _submit(state)
    assert result["status"] == "submitted"
    wf = fake.submitted

    load_node = next(n for n in wf.values() if n.get("class_type") == "LoadImage")
    assert load_node["inputs"]["image"] == "srv_first.png"
    seedance = next(n for n in wf.values() if "JimengSeedance" in n.get("class_type", ""))
    assert seedance["inputs"]["last_frame_image"] == ["__last_frame", 0]
    assert wf["__last_frame"]["inputs"]["image"] == "srv_last.png"
    assert seedance["inputs"]["ref_videos"] == {"ref_video_1": ["__ref_vid0", 0]}
    assert wf["__ref_vid0"]["inputs"]["file"] == "srv_ref.mp4"