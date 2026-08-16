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
