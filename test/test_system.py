"""系统状态 API 测试"""
import pytest

from web.routers import system as system_router


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """避免真实网络请求，ComfyUI 检测固定为离线"""

    async def fake_check(host):
        return {"online": False, "host": host, "error": "mock offline"}

    monkeypatch.setattr(system_router, "_check_comfyui", fake_check)


def test_status_empty(client, workdir):
    r = client.get("/api/system/status")
    assert r.status_code == 200
    data = r.json()
    assert data["comfyui"]["online"] is False
    assert data["comfyui"]["host"] == "http://127.0.0.1:8188"
    assert "output_size_mb" in data["disk"]
    assert data["latest_batch"] is None
    assert "timestamp" in data


def test_status_disk_size_counts_output(client, workdir):
    out = workdir / "output"
    (out / "batch_001").mkdir(parents=True)
    (out / "batch_001" / "a.mp4").write_bytes(b"0" * (2 * 1024 * 1024))
    data = client.get("/api/system/status").json()
    assert data["disk"]["output_size_mb"] > 0


def test_status_latest_batch(client, workdir):
    out = workdir / "output"
    shots = out / "batch_20260813_100000" / "shots"
    final_dir = out / "batch_20260813_100000" / "final"
    shots.mkdir(parents=True)
    final_dir.mkdir(parents=True)
    (shots / "a.mp4").write_bytes(b"a")
    (shots / "b.mp4").write_bytes(b"b")
    (final_dir / "final_video.mp4").write_bytes(b"f")

    data = client.get("/api/system/status").json()
    latest = data["latest_batch"]
    assert latest["batch_id"] == "batch_20260813_100000"
    assert latest["shots_count"] == 2
    assert latest["has_final"] is True
    assert "created_at" in latest


def test_status_uses_config_host(client, workdir, monkeypatch):
    import yaml

    cfg = yaml.safe_load((workdir / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("comfyui", {})["host"] = "http://192.168.1.10:8188"
    (workdir / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    data = client.get("/api/system/status").json()
    assert data["comfyui"]["host"] == "http://192.168.1.10:8188"