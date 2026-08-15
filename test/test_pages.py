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


def test_frontend_media_paths_are_relative_to_output(client):
    """视频 API 已以 output/ 为根；前端不可再额外拼 output/。"""
    html = client.get("/").text
    normalized = html.replace(" ", "")
    assert "path:'output/'+v.path" not in normalized
    assert 'path:"output/"+v.path' not in normalized
    assert "path:v.path" in normalized


def test_static_missing_file(client):
    r = client.get("/static/no-such-file.js")
    assert r.status_code == 404


def test_unknown_page(client):
    assert client.get("/no-such-page").status_code == 404
