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


def test_static_missing_file(client):
    r = client.get("/static/no-such-file.js")
    assert r.status_code == 404


def test_unknown_page(client):
    assert client.get("/no-such-page").status_code == 404
