"""视频预览 API 测试"""
from pathlib import Path


def test_list_empty(client):
    r = client.get("/api/videos")
    assert r.status_code == 200
    assert r.json() == {"videos": [], "batches": {}, "total": 0, "hidden_total": 0}


def test_list_groups_by_batch(client, workdir):
    out = workdir / "output"
    (out / "batch_001" / "shots").mkdir(parents=True)
    (out / "batch_002").mkdir(parents=True)
    (out / "batch_001" / "shots" / "a.mp4").write_bytes(b"a")
    (out / "batch_002" / "b.mp4").write_bytes(b"b")
    (out / "root.mp4").write_bytes(b"c")

    r = client.get("/api/videos")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert set(data["batches"]) == {"batch_001", "batch_002", "root"}
    assert data["batches"]["batch_001"][0]["path"] == "batch_001/shots/a.mp4"


def test_stream_full(client, workdir):
    (workdir / "output").mkdir()
    (workdir / "output" / "a.mp4").write_bytes(b"0123456789")
    r = client.get("/api/videos/a.mp4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == b"0123456789"


def test_stream_range(client, workdir):
    (workdir / "output").mkdir()
    (workdir / "output" / "a.mp4").write_bytes(b"0123456789")
    r = client.get("/api/videos/a.mp4", headers={"Range": "bytes=2-5"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 2-5/10"
    assert r.content == b"2345"


def test_stream_range_suffix(client, workdir):
    (workdir / "output").mkdir()
    (workdir / "output" / "a.mp4").write_bytes(b"0123456789")
    r = client.get("/api/videos/a.mp4", headers={"Range": "bytes=-3"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 7-9/10"
    assert r.content == b"789"


def test_stream_range_open_ended(client, workdir):
    (workdir / "output").mkdir()
    (workdir / "output" / "a.mp4").write_bytes(b"0123456789")
    r = client.get("/api/videos/a.mp4", headers={"Range": "bytes=7-"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 7-9/10"
    assert r.content == b"789"


def test_stream_non_media(client, workdir):
    (workdir / "output").mkdir()
    (workdir / "output" / "a.txt").write_text("x")
    r = client.get("/api/videos/a.txt")
    assert r.status_code == 400


def test_stream_missing(client, workdir):
    (workdir / "output").mkdir()
    r = client.get("/api/videos/no_such.mp4")
    assert r.status_code == 404


# ── 图片素材 ─────────────────────────────────────────

def test_list_includes_images(client, workdir):
    out = workdir / "output"
    (out / "batch_img" / "images").mkdir(parents=True)
    (out / "batch_img" / "images" / "gen.png").write_bytes(b"x")
    (out / "batch_img" / "shots").mkdir(parents=True)
    (out / "batch_img" / "shots" / "a.mp4").write_bytes(b"a")

    data = client.get("/api/videos").json()
    by_path = {m["path"]: m for m in data["videos"]}
    assert by_path["batch_img/images/gen.png"]["type"] == "image"
    assert by_path["batch_img/shots/a.mp4"]["type"] == "video"


def test_assets_dir_excluded(client, workdir):
    (workdir / "output" / "assets" / "images").mkdir(parents=True)
    (workdir / "output" / "assets" / "images" / "up.png").write_bytes(b"x")
    (workdir / "output" / "b.mp4").write_bytes(b"b")

    data = client.get("/api/videos").json()
    assert data["total"] == 1
    assert all("assets/" not in m["path"] for m in data["videos"])


def test_stream_image(client, workdir):
    (workdir / "output").mkdir()
    (workdir / "output" / "a.png").write_bytes(b"img-bytes")
    r = client.get("/api/videos/a.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == b"img-bytes"


# ── 隐藏 / 恢复（软删除） ─────────────────────────────

def test_hide_video(client, workdir):
    (workdir / "output" / "batch_1" / "shots").mkdir(parents=True)
    f = workdir / "output" / "batch_1" / "shots" / "a.mp4"
    f.write_bytes(b"x")

    r = client.post("/api/videos/hide", json={"path": "batch_1/shots/a.mp4"})
    assert r.status_code == 200
    # 文件仍存在
    assert f.exists()
    # 列表消失
    data = client.get("/api/videos").json()
    assert data["total"] == 0
    assert data["hidden_total"] == 1
    # include_hidden=1 可看到
    data = client.get("/api/videos", params={"include_hidden": 1}).json()
    assert [m["path"] for m in data["hidden"]] == ["batch_1/shots/a.mp4"]


def test_hide_persists_across_requests(client, workdir):
    (workdir / "output" / "batch_1" / "shots").mkdir(parents=True)
    (workdir / "output" / "batch_1" / "shots" / "a.mp4").write_bytes(b"x")
    client.post("/api/videos/hide", json={"path": "batch_1/shots/a.mp4"})
    client.post("/api/videos/hide", json={"path": "batch_1/shots/a.mp4"})  # 幂等
    data = client.get("/api/videos", params={"include_hidden": 1}).json()
    assert len(data["hidden"]) == 1


def test_unhide_video(client, workdir):
    (workdir / "output" / "batch_1" / "shots").mkdir(parents=True)
    f = workdir / "output" / "batch_1" / "shots" / "a.mp4"
    f.write_bytes(b"x")
    client.post("/api/videos/hide", json={"path": "batch_1/shots/a.mp4"})

    r = client.post("/api/videos/unhide", json={"path": "batch_1/shots/a.mp4"})
    assert r.status_code == 200
    data = client.get("/api/videos").json()
    assert data["total"] == 1
    assert data["hidden_total"] == 0


def test_hide_invalid_path(client, workdir):
    (workdir / "output").mkdir()
    r = client.post("/api/videos/hide", json={"path": "no_such.mp4"})
    assert r.status_code == 404
    r = client.post("/api/videos/hide", json={"path": "../secret.mp4"})
    assert r.status_code == 400


def test_unhide_not_hidden(client, workdir):
    (workdir / "output").mkdir()
    (workdir / "output" / "a.mp4").write_bytes(b"x")
    r = client.post("/api/videos/unhide", json={"path": "a.mp4"})
    assert r.status_code == 404


def test_ghost_hidden_record_cleaned(client, workdir):
    """隐藏后文件被物理删除 → 幽灵记录自动清理"""
    (workdir / "output" / "batch_1" / "shots").mkdir(parents=True)
    f = workdir / "output" / "batch_1" / "shots" / "a.mp4"
    f.write_bytes(b"x")
    client.post("/api/videos/hide", json={"path": "batch_1/shots/a.mp4"})
    f.unlink()
    # 再次 hide 另一个文件时，幽灵记录被清理
    (workdir / "output" / "batch_1" / "shots" / "b.mp4").write_bytes(b"x")
    client.post("/api/videos/hide", json={"path": "batch_1/shots/b.mp4"})
    data = client.get("/api/videos", params={"include_hidden": 1}).json()
    assert [m["path"] for m in data["hidden"]] == ["batch_1/shots/b.mp4"]


# ── 导入 / 物理删除 ─────────────────────────────────────────

def test_import_video(client, workdir):
    files = {"file": ("clip.mp4", b"fake-mp4-bytes", "video/mp4")}
    r = client.post("/api/videos/import", files=files)
    assert r.status_code == 200
    path = r.json()["path"]
    assert path.startswith("imported/")

    videos = client.get("/api/videos").json()
    assert videos["total"] == 1
    assert videos["videos"][0]["kind"] == "imported"
    assert videos["batches"].get("导入")


def test_import_invalid_ext(client):
    files = {"file": ("clip.txt", b"x", "text/plain")}
    r = client.post("/api/videos/import", files=files)
    assert r.status_code == 400


def test_delete_imported(client, workdir):
    (workdir / "output" / "imported").mkdir(parents=True)
    (workdir / "output" / "imported" / "a.mp4").write_bytes(b"x")
    r = client.delete("/api/videos", params={"path": "imported/a.mp4"})
    assert r.status_code == 200
    assert not (workdir / "output" / "imported" / "a.mp4").exists()


def test_delete_generated_forbidden(client, workdir):
    """生成视频不能物理删除（应走 hide）"""
    (workdir / "output" / "batch_1" / "shots").mkdir(parents=True)
    (workdir / "output" / "batch_1" / "shots" / "a.mp4").write_bytes(b"x")
    r = client.delete("/api/videos", params={"path": "batch_1/shots/a.mp4"})
    assert r.status_code == 400
    assert (workdir / "output" / "batch_1" / "shots" / "a.mp4").exists()


def test_dialogue_from_manifest(client, workdir):
    import json
    out = workdir / "output"
    (out / "batch_x" / "shots").mkdir(parents=True)
    (out / "batch_x" / "shots" / "batch_x_shot_1.mp4").write_bytes(b"x")
    (out / "batch_x" / "generate_manifest.json").write_text(
        json.dumps({"1": {"table_id": "1", "shot_id": "4", "dialogue": "你好世界", "screen_text": ""}}),
        encoding="utf-8")

    videos = client.get("/api/videos").json()
    assert videos["videos"][0]["dialogue"] == "你好世界"


# ── 批次打包下载 ─────────────────────────────────────────

def test_download_batch_excludes_hidden(client, workdir):
    import io
    import zipfile as zf

    out = workdir / "output"
    (out / "batch_z" / "shots").mkdir(parents=True)
    (out / "batch_z" / "shots" / "keep.mp4").write_bytes(b"k")
    (out / "batch_z" / "shots" / "bad.mp4").write_bytes(b"b")
    client.post("/api/videos/hide", json={"path": "batch_z/shots/bad.mp4"})

    r = client.get("/api/batches/batch_z/download")
    assert r.status_code == 200
    with zf.ZipFile(io.BytesIO(r.content)) as z:
        names = z.namelist()
    assert any("keep.mp4" in n for n in names)
    assert not any("bad.mp4" in n for n in names)
