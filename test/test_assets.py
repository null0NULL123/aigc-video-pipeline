"""图片素材库 API 测试"""
import io


def test_list_empty(client):
    r = client.get("/api/assets/images")
    assert r.status_code == 200
    assert r.json() == {"images": [], "total": 0}


def test_upload_and_list(client, workdir):
    files = {"file": ("hero.png", b"png-bytes", "image/png")}
    r = client.post("/api/assets/images", files=files)
    assert r.status_code == 200
    path = r.json()["path"]
    assert path.startswith("output/assets/images/")
    assert (workdir / path).exists()

    data = client.get("/api/assets/images").json()
    assert data["total"] == 1
    assert data["images"][0]["path"] == path


def test_upload_invalid_ext(client):
    files = {"file": ("a.txt", b"x", "text/plain")}
    assert client.post("/api/assets/images", files=files).status_code == 400


def test_stream_image(client, workdir):
    (workdir / "output" / "assets" / "images").mkdir(parents=True)
    p = workdir / "output" / "assets" / "images" / "a.png"
    p.write_bytes(b"0123456789")
    path = "output/assets/images/a.png"

    r = client.get(f"/api/assets/images/{path}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == b"0123456789"

    r2 = client.get(f"/api/assets/images/{path}", headers={"Range": "bytes=2-5"})
    assert r2.status_code == 206
    assert r2.content == b"2345"


def test_stream_outside_assets_forbidden(client, workdir):
    (workdir / "output" / "imported").mkdir(parents=True)
    (workdir / "output" / "imported" / "a.mp4").write_bytes(b"x")
    r = client.get("/api/assets/images/output/imported/a.mp4")
    assert r.status_code == 400


def test_delete_image(client, workdir):
    (workdir / "output" / "assets" / "images").mkdir(parents=True)
    p = workdir / "output" / "assets" / "images" / "a.png"
    p.write_bytes(b"x")
    path = "output/assets/images/a.png"

    r = client.delete("/api/assets/images", params={"path": path})
    assert r.status_code == 200
    assert not p.exists()

    r2 = client.delete("/api/assets/images", params={"path": path})
    assert r2.status_code == 404


def test_delete_outside_assets_forbidden(client, workdir):
    (workdir / "output" / "imported").mkdir(parents=True)
    (workdir / "output" / "imported" / "a.mp4").write_bytes(b"x")
    r = client.delete("/api/assets/images", params={"path": "output/imported/a.mp4"})
    assert r.status_code == 400