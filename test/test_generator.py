"""生成链路测试：LLM 开启/关闭/失败时的 prompt 与回退"""
import pytest

from pipeline.generator import _polish, _optimize_prompt

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