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


def test_status_latest_batch_new_structure(client, workdir):
    """新结构 output/{category}/{batch_id}/ 也能被 _latest_batch 识别"""
    out = workdir / "output"
    shots = out / "shots" / "demo_2026_new"
    final_dir = out / "final" / "demo_2026_new"
    shots.mkdir(parents=True)
    final_dir.mkdir(parents=True)
    (shots / "a.mp4").write_bytes(b"a")
    (shots / "b.mp4").write_bytes(b"b")
    (shots / "c.mp4").write_bytes(b"c")
    (final_dir / "final_video.mp4").write_bytes(b"f")

    data = client.get("/api/system/status").json()
    latest = data["latest_batch"]
    assert latest["batch_id"] == "demo_2026_new"
    assert latest["shots_count"] == 3
    assert latest["has_final"] is True


def test_discover_batches_mixed_structures(client, workdir):
    """新结构 + 旧结构 batch 混合存在时都能发现"""
    from web.routers.system import _discover_batches

    out = workdir / "output"
    # 新结构
    (out / "shots" / "demo_new").mkdir(parents=True)
    # 旧结构
    (out / "batch_20260817_999999" / "shots").mkdir(parents=True)

    names = {b.name for b in _discover_batches()}
    assert "demo_new" in names
    assert "batch_20260817_999999" in names


def test_resolve_batch_paths_prefers_new_structure(client, workdir):
    """新旧结构同时存在时优先新结构"""
    from web.routers.system import _resolve_batch_paths

    out = workdir / "output"
    batch_name = "demo_both"
    # 旧结构（不应被选中）
    old_shots = out / batch_name / "shots"
    old_shots.mkdir(parents=True)
    (old_shots / "old.mp4").write_bytes(b"old")
    # 新结构（应被选中）
    new_shots = out / "shots" / batch_name
    new_shots.mkdir(parents=True)
    (new_shots / "new_a.mp4").write_bytes(b"new_a")
    (new_shots / "new_b.mp4").write_bytes(b"new_b")

    batch_path = out / batch_name
    shots, final = _resolve_batch_paths(batch_path)
    assert shots == new_shots, f"new structure should win, got {shots}"
    assert len(list(shots.glob("*.mp4"))) == 2