"""表格 + 镜头管理 API 测试"""
import io
import json

CSV_CONTENT = (
    "id,duration,画面内容,台词,屏幕字幕,素材来源,素材路径\n"
    "1,5,海边日落,你好世界,字幕A,image,input/a.png\n"
    "2,3,城市夜景,晚安,字幕B,ai_generated,\n"
)


def _make_shot(**overrides):
    base = {
        "duration": 4,
        "scene_desc": "默认场景",
        "dialogue": "默认台词",
        "screen_text": "",
        "asset_type": "ai_generated",
        "asset_path": "",
    }
    base.update(overrides)
    return base


# ── 表格 CRUD ───────────────────────────────────────────

def test_list_tables_empty(client):
    r = client.get("/api/tables")
    assert r.status_code == 200
    assert r.json() == []


def test_create_and_rename_table(client):
    r = client.post("/api/tables", json={"name": "宣传片"})
    assert r.status_code == 200
    t = r.json()
    assert t["id"] == "1"
    assert t["name"] == "宣传片"

    r = client.put("/api/tables/1", json={"name": "宣传片V2"})
    assert r.json()["name"] == "宣传片V2"

    tables = client.get("/api/tables").json()
    assert tables[0]["name"] == "宣传片V2"
    assert tables[0]["shot_count"] == 0


def test_delete_table(client):
    client.post("/api/tables", json={"name": "A"})
    r = client.delete("/api/tables/1")
    assert r.json()["ok"] is True
    assert client.get("/api/tables").json() == []
    assert client.delete("/api/tables/1").status_code == 404


# ── 镜头 CRUD（按表格）──────────────────────────────────

def test_shots_empty(client):
    client.post("/api/tables", json={"name": "T"})
    r = client.get("/api/tables/1/shots")
    assert r.status_code == 200
    assert r.json() == []


def test_create_shot_in_table(client):
    client.post("/api/tables", json={"name": "T"})
    r = client.post("/api/tables/1/shots", json=_make_shot(scene_desc="海边日落"))
    assert r.status_code == 200
    shot = r.json()
    assert shot["id"] == "1"
    assert shot["status"] == "pending"

    shots = client.get("/api/tables/1/shots").json()
    assert len(shots) == 1
    assert shots[0]["scene_desc"] == "海边日落"


def test_shots_scoped_per_table(client):
    client.post("/api/tables", json={"name": "T1"})
    client.post("/api/tables", json={"name": "T2"})
    client.post("/api/tables/1/shots", json=_make_shot(scene_desc="A"))
    client.post("/api/tables/2/shots", json=_make_shot(scene_desc="B"))

    # 两个表格都有 id=1 的镜头，互不影响
    assert client.get("/api/tables/1/shots").json()[0]["scene_desc"] == "A"
    assert client.get("/api/tables/2/shots").json()[0]["scene_desc"] == "B"


def test_update_shot(client):
    client.post("/api/tables", json={"name": "T"})
    created = client.post("/api/tables/1/shots", json=_make_shot()).json()
    r = client.put(f"/api/tables/1/shots/{created['id']}", json=_make_shot(duration=8, scene_desc="已编辑"))
    assert r.status_code == 200
    assert r.json()["duration"] == 8


def test_update_missing_shot(client):
    client.post("/api/tables", json={"name": "T"})
    assert client.put("/api/tables/1/shots/999", json=_make_shot()).status_code == 404


def test_delete_shot(client):
    client.post("/api/tables", json={"name": "T"})
    created = client.post("/api/tables/1/shots", json=_make_shot()).json()
    r = client.delete(f"/api/tables/1/shots/{created['id']}")
    assert r.json()["ok"] is True
    assert client.get("/api/tables/1/shots").json() == []


def test_reorder_shots(client):
    client.post("/api/tables", json={"name": "T"})
    a = client.post("/api/tables/1/shots", json=_make_shot(scene_desc="A")).json()
    b = client.post("/api/tables/1/shots", json=_make_shot(scene_desc="B")).json()
    c = client.post("/api/tables/1/shots", json=_make_shot(scene_desc="C")).json()
    r = client.put("/api/tables/1/shots/reorder", json={"ordered_ids": [c["id"], a["id"], b["id"]]})
    assert r.status_code == 200
    ids = [s["id"] for s in client.get("/api/tables/1/shots").json()]
    assert ids == [c["id"], a["id"], b["id"]]


# ── 导入导出 ─────────────────────────────────────────────

def test_import_csv(client):
    client.post("/api/tables", json={"name": "T"})
    files = {"file": ("shots.csv", io.BytesIO(CSV_CONTENT.encode("utf-8")), "text/csv")}
    r = client.post("/api/tables/1/import", files=files)
    assert r.status_code == 200
    assert r.json()["count"] == 2

    shots = client.get("/api/tables/1/shots").json()
    assert shots[0]["scene_desc"] == "海边日落"
    assert shots[0]["duration"] == 5
    assert shots[1]["scene_desc"] == "城市夜景"


def test_import_invalid_ext(client):
    client.post("/api/tables", json={"name": "T"})
    files = {"file": ("shots.txt", io.BytesIO(b"x"), "text/plain")}
    r = client.post("/api/tables/1/import", files=files)
    assert r.status_code == 400


def test_export_empty_404(client):
    client.post("/api/tables", json={"name": "T"})
    r = client.get("/api/tables/1/export")
    assert r.status_code == 404


def test_export_csv(client):
    client.post("/api/tables", json={"name": "T"})
    client.post("/api/tables/1/shots", json=_make_shot(scene_desc="海边日落", duration=5))
    r = client.get("/api/tables/1/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.content.decode("utf-8")
    assert "画面内容" in body
    assert "海边日落" in body


# ── 迁移 ─────────────────────────────────────────────────

def test_migrate_web_shots(client, workdir):
    old = workdir / "input" / "web_shots.json"
    old.write_text(json.dumps([
        {"id": "1", "duration": 4, "scene_desc": "旧镜头", "dialogue": "台词",
         "screen_text": "", "asset_type": "ai_generated", "asset_path": "", "status": "pending"},
    ]), encoding="utf-8")

    tables = client.get("/api/tables").json()
    assert len(tables) == 1
    assert tables[0]["name"] == "默认表格"
    assert tables[0]["shot_count"] == 1

    shots = client.get(f"/api/tables/{tables[0]['id']}/shots").json()
    assert shots[0]["scene_desc"] == "旧镜头"
    assert not old.exists()
