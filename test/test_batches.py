"""批次打包下载 API 测试"""
import io
import zipfile


def test_download_batch(client, workdir):
    out = workdir / "output"
    (out / "batch_001" / "shots").mkdir(parents=True)
    (out / "batch_001" / "final").mkdir(parents=True)
    (out / "batch_001" / "shots" / "a.mp4").write_bytes(b"video-a")
    (out / "batch_001" / "final" / "batch_001.mp4").write_bytes(b"final-v")

    r = client.get("/api/batches/batch_001/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "batch_001/shots/a.mp4" in names
    assert "batch_001/final/batch_001.mp4" in names
    assert zf.read("batch_001/shots/a.mp4") == b"video-a"


def test_download_batch_missing(client):
    assert client.get("/api/batches/no_such/download").status_code == 404


def test_download_batch_empty(client, workdir):
    (workdir / "output" / "batch_001").mkdir(parents=True)
    r = client.get("/api/batches/batch_001/download")
    assert r.status_code == 404