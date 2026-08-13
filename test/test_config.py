"""配置管理 API 测试"""


def test_get_config(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert data["logging"]["level"] == "INFO"
    assert data["llm"]["model"] == "mimo-v2.5"


def test_save_config(client, workdir):
    r = client.put("/api/config", json={"logging": {"level": "DEBUG"}, "note": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "config.yaml.bak." in body["backup"]
    # 内容已写入
    assert "DEBUG" in (workdir / "config.yaml").read_text(encoding="utf-8")
    # 固定备份与时间戳备份都已生成
    assert (workdir / "config.yaml.bak").exists()
    assert (workdir / body["backup"]).exists()


def test_save_config_without_existing(client, workdir):
    (workdir / "config.yaml").unlink()
    r = client.put("/api/config", json={"logging": {"level": "INFO"}})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["backup"] is None
    assert (workdir / "config.yaml").exists()


def test_reset_config(client):
    client.put("/api/config", json={"logging": {"level": "DEBUG"}})
    r = client.post("/api/config/reset")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["config"]["logging"]["level"] == "INFO"
    assert client.get("/api/config").json()["logging"]["level"] == "INFO"


def test_reset_config_no_backup(client, workdir):
    (workdir / "config.yaml.bak").unlink(missing_ok=True)
    r = client.post("/api/config/reset")
    assert r.status_code == 404
