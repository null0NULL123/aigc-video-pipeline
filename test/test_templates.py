"""模板管理 API 测试"""
import io
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_list_templates(client):
    r = client.get("/api/templates")
    assert r.status_code == 200
    tpls = r.json()
    expected = len(list((PROJECT_ROOT / "templates").glob("*.json")))
    assert len(tpls) == expected
    assert tpls  # 至少有一个模板
    for t in tpls:
        assert {"id", "name", "category", "description", "workflow_type", "file"} <= set(t)


def test_get_template_by_id(client):
    tpls = client.get("/api/templates").json()
    first = tpls[0]
    r = client.get(f"/api/templates/{first['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == first["id"]


def test_get_missing_template(client):
    r = client.get("/api/templates/no_such_template")
    assert r.status_code == 404


def test_import_template(client, workdir):
    tpl = {"id": "my_tpl", "name": "我的模板", "workflow_type": "seedance"}
    files = {"file": ("my_tpl.json", io.BytesIO(json.dumps(tpl).encode("utf-8")), "application/json")}
    r = client.post("/api/templates/import", files=files)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert (workdir / "templates" / "my_tpl.json").exists()

    ids = [t["id"] for t in client.get("/api/templates").json()]
    assert "my_tpl" in ids
    assert client.get("/api/templates/my_tpl").json()["name"] == "我的模板"


def test_import_invalid_json(client):
    files = {"file": ("bad.json", io.BytesIO(b"{not json"), "application/json")}
    r = client.post("/api/templates/import", files=files)
    assert r.status_code == 400


def test_import_wrong_ext(client):
    files = {"file": ("tpl.txt", io.BytesIO(b"{}"), "text/plain")}
    r = client.post("/api/templates/import", files=files)
    assert r.status_code == 400
