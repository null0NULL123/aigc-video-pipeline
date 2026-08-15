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


# ---------- Seedream 模板结构 ----------

def _load_template(name):
    with open(PROJECT_ROOT / "templates" / name, encoding="utf-8") as f:
        return json.load(f)


def test_seedream_t2i_structure():
    """T2I 模板：Seedream4 + APIClient + SaveImage，必须能导出图片"""
    tpl = _load_template("seedream_t2i.json")
    wf = tpl["workflow"]
    cts = {n["class_type"] for n in wf.values()}
    assert "JimengSeedream4" in cts
    assert "SaveImage" in cts, "缺少 SaveImage，无法导出图片"
    assert "JimengAPIClient" in cts
    assert tpl["match_rules"]["asset_type"] == ["ai_generated", "none", ""]
    assert tpl["usage"]["output"] == "image"

    seedream = next(n for n in wf.values() if n["class_type"] == "JimengSeedream4")
    save = next(n for n in wf.values() if n["class_type"] == "SaveImage")
    assert seedream["inputs"]["images"] == {}, "images 是必需输入"
    assert save["inputs"]["images"][0] == "1", "SaveImage 应接入 Seedream4 输出"


def test_seedream_t2i2v_structure():
    """链式模板：Seedream4 首帧 → Seedance2 动画 → SaveVideo"""
    tpl = _load_template("seedream_t2i2v.json")
    wf = tpl["workflow"]
    cts = {n["class_type"] for n in wf.values()}
    assert "JimengSeedream4" in cts
    assert "JimengSeedance2" in cts
    assert "SaveVideo" in cts
    assert "SaveImage" not in cts, "链式只需输出视频，不应 SaveImage"

    seedance = next(n for n in wf.values() if n["class_type"] == "JimengSeedance2")
    assert seedance["inputs"]["first_frame_image"] == ["1", 0], \
        "Seedance2 首帧应直接接 Seedream4 的 IMAGE 输出"
    assert tpl["usage"].get("dual_prompt") is True


def test_registry_loads_new_templates(client):
    """两个新模板都被注册表加载"""
    tpls = client.get("/api/templates").json()
    ids = {t["id"] for t in tpls}
    assert "seedream_t2i" in ids
    assert "seedream_t2i2v" in ids


def test_list_templates_include_match_rules(client):
    """模板列表返回 match_rules 供前端按素材类型过滤工作流"""
    tpls = client.get("/api/templates").json()
    by_id = {t["id"]: t for t in tpls}
    assert "match_rules" in by_id["seedance_i2v"]
    assert by_id["seedance_i2v"]["match_rules"]["asset_type"] == ["image", "local"]
    assert by_id["seedance_t2v"]["match_rules"]["asset_type"] == ["ai_generated", "none", ""]


def test_find_best_t2i_by_keyword():
    """场景含海报/图片关键词 → 选 seedream_t2i"""
    from pipeline.registry import TemplateRegistry
    reg = TemplateRegistry(str(PROJECT_ROOT / "templates"), config={})
    assert reg.find_best(asset_type="ai_generated", scene_desc="产品宣传海报") == "seedream_t2i"


def test_find_best_t2i2v_by_keyword():
    """场景含动画/动态关键词 → 选 seedream_t2i2v"""
    from pipeline.registry import TemplateRegistry
    reg = TemplateRegistry(str(PROJECT_ROOT / "templates"), config={})
    assert reg.find_best(asset_type="ai_generated", scene_desc="产品旋转动画") == "seedream_t2i2v"


def test_find_best_default_t2v_unchanged():
    """无关键词的普通场景仍走 seedance_t2v"""
    from pipeline.registry import TemplateRegistry
    reg = TemplateRegistry(str(PROJECT_ROOT / "templates"), config={})
    assert reg.find_best(asset_type="ai_generated", scene_desc="产品特写镜头") == "seedance_t2v"
