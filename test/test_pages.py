"""页面与静态资源测试"""


def test_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "aigc-video" in r.text


def test_static_index(client):
    r = client.get("/static/index.html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_frontend_simplified_navigation_has_no_dub_or_system_pages(client):
    """配音字幕/系统是后端能力，不应再作为独立主导航页面。"""
    html = client.get("/").text
    assert 'index="dub"' not in html
    assert 'index="system"' not in html
    assert "activeTab==='dub'" not in html
    assert "activeTab==='system'" not in html
    assert "/api/dub" not in html


def test_navigation_has_two_type_groups(client):
    """素材库导航必须是顶级 + 子级 两层结构，分为视频素材/图片素材两组。"""
    html = client.get("/").text
    js = client.get("/static/main.js").text
    assert js, "main.js 应当可访问"
    # 模板: 有 nav-group 顶级组容器 + nav-children 子级容器
    assert "nav-group" in html, "模板没有 nav-group 顶级组样式"
    assert "nav-children" in html, "模板没有 nav-children 子级容器"
    # main.js: 数据层定义了'视频素材'/'图片素材'两个顶级组
    assert "'视频素材'" in js, "main.js 没有'视频素材'顶级组"
    assert "'图片素材'" in js, "main.js 没有'图片素材'顶级组"
    # 顶级组 id 用 type:video / type:image
    assert "'type:video'" in js, "main.js 没有 type:video 顶级 id"
    assert "'type:image'" in js, "main.js 没有 type:image 顶级 id"
    # 子级 batch id 用 b: 前缀
    assert "'b:'+batchId" in js, "main.js 没有 batchId 前缀逻辑"


def test_topbar_menu_is_data_driven(client):
    """顶部菜单必须由 menuItems 数据驱动，不允许重新硬编码漏掉某个 tab。"""
    html = client.get("/").text
    js = client.get("/static/main.js").text
    assert js, "main.js 应当可访问"

    # 模板里必须用 v-for 渲染菜单
    assert 'v-for="m in menuItems"' in html, "顶部菜单没切换到 v-for 渲染"

    # 模板里不应再硬编码 5 个 index（A 图像通过 v-for 渲染 → 模板里 0 个 index="X"）
    for idx in ("generate", "images", "library", "merge", "settings"):
        assert ('index="' + idx + '"') not in html, (
            "顶部菜单发现硬编码 index=" + idx + "，应当由 v-for 渲染"
        )

    # menuItems 数组必须包含 5 项；少一个就退化
    for idx in ("generate", "images", "library", "merge", "settings"):
        assert ("index:'" + idx + "'") in js, "menuItems 缺少 " + idx


def test_frontend_media_paths_are_relative_to_output(client):
    """视频 API 已以 output/ 为根；前端不可再额外拼 output/。"""
    html = client.get("/").text
    normalized = html.replace(" ", "")
    assert "path:'output/'+v.path" not in normalized
    assert 'path:"output/"+v.path' not in normalized
    # main.js 是外置脚本，也要检查同样的规则
    js = client.get("/static/main.js").text
    assert js, "main.js 应当可访问"
    assert "path:'output/'+v.path" not in js
    assert 'path:"output/"+v.path' not in js
    assert "path:v.path" in js


def test_static_missing_file(client):
    r = client.get("/static/no-such-file.js")
    assert r.status_code == 404


def test_unknown_page(client):
    assert client.get("/no-such-page").status_code == 404


# 0 引用字段白名单：UI 只允许绑定被后端读取的字段。
# 任何这里列出的字段如果不再被后端使用，应从 UI 中移除，并从下方白名单删去。
FRONTEND_CFG_WHITELIST = frozenset({
    # LLM
    "cfg.llm.enabled", "cfg.llm.api_url", "cfg.llm.api_key", "cfg.llm.model",
    "cfg.llm.max_tokens", "cfg.llm.temperature", "cfg.llm.timeout",
    # Seedance（volcano.py 真实使用 model_version/resolution/aspect_ratio/default_duration/generate_audio/enable_random_seed）
    "cfg.seedance.model_version", "cfg.seedance.resolution",
    "cfg.seedance.aspect_ratio", "cfg.seedance.default_duration",
    "cfg.seedance.generate_audio", "cfg.seedance.enable_random_seed",
    # FFmpeg（merge.py + media.py）
    "cfg.ffmpeg.crf", "cfg.ffmpeg.pix_fmt", "cfg.ffmpeg.font_family",
    "cfg.ffmpeg.font_size", "cfg.ffmpeg.audio_codec", "cfg.ffmpeg.audio_bitrate",
    "cfg.ffmpeg.subtitle.font_color", "cfg.ffmpeg.subtitle.outline_color",
    "cfg.ffmpeg.subtitle.outline_width", "cfg.ffmpeg.subtitle.alignment",
    "cfg.ffmpeg.subtitle.margin_v", "cfg.ffmpeg.subtitle.shadow",
    # TTS
    "cfg.tts.voice", "cfg.tts.rate",
    # Merge（merge.py 真实使用 transition/transition_duration/tts_mode/break_between_shots_ms/silent_sample_rate/concat_filename）
    "cfg.merge.transition", "cfg.merge.transition_duration",
    "cfg.merge.tts_mode", "cfg.merge.break_between_shots_ms",
    "cfg.merge.silent_sample_rate", "cfg.merge.concat_filename",
    # Output
    "cfg.output.shots_dir", "cfg.output.audio_dir", "cfg.output.subs_dir",
    "cfg.output.merged_dir", "cfg.output.final_dir",
    # Input
    "cfg.input.default_duration",
    # Agent（generator.py 真实使用 default_seed/default_duration/max_retries/duration_range/prompt_*）
    "cfg.agent.default_seed", "cfg.agent.default_duration",
    "cfg.agent.max_retries", "cfg.agent.duration_range",
    "cfg.agent.prompt_min_length", "cfg.agent.prompt_short_duration",
    "cfg.agent.prompt_long_duration",
})


def test_settings_no_zero_reference_fields(client):
    """设置页不能出现后端 0 引用的 cfg 字段（除 keywordMapText 文本编辑器外）。"""
    import re
    html = client.get("/").text
    # 提取所有 v-model="cfg.xxx.yyy..." 绑定（不包括带 .length / .map 之类方法调用）
    matches = re.findall(r'v-model="(cfg\.[\w.\[\]0-9]+?)"', html)
    # 去重 + 过滤：cfg.agent.duration_range[0]/[1] 这类
    clean = sorted(set(matches))
    # 数组下标视为白名单前缀（duration_range[0] / duration_range[1] 都归到 duration_range）
    def whitelisted(m):
        if m in FRONTEND_CFG_WHITELIST:
            return True
        return any(m.startswith(base + "[") for base in FRONTEND_CFG_WHITELIST)
    offenders = [m for m in clean if not whitelisted(m)]
    assert not offenders, (
        "设置页出现后端 0 引用的字段，请先确认后端是否真的不用，"
        "再从 index.html 删除或加到 FRONTEND_CFG_WHITELIST：\n  " + "\n  ".join(offenders)
    )
