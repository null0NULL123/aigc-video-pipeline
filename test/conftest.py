"""pytest 共享 fixtures"""
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_YAML = """\
logging:
  level: INFO
  dir: output/logs
llm:
  api_url: https://api.example.com/v1
  api_key: test-key
  model: mimo-v2.5
  max_tokens: 1024
  temperature: 0.8
"""


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """把所有 web 路径重定向到临时目录，隔离真实文件"""
    from web import settings

    monkeypatch.setattr(settings, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(settings, "BACKUP_PATH", tmp_path / "config.yaml.bak")
    monkeypatch.setattr(settings, "TEMPLATE_DIR", tmp_path / "templates")
    monkeypatch.setattr(settings, "SHOTS_FILE", tmp_path / "input" / "web_shots.json")
    monkeypatch.setattr(settings, "TABLES_FILE", tmp_path / "input" / "tables.json")
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(settings, "IMPORTED_DIR", tmp_path / "output" / "imported")
    monkeypatch.setattr(settings, "PIPELINE_CWD", tmp_path)
    (tmp_path / "input").mkdir()
    return tmp_path


@pytest.fixture()
def client(workdir):
    """在临时工作目录上构建 TestClient（含默认 config.yaml 和模板文件）"""
    (workdir / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")

    tpl_dir = workdir / "templates"
    tpl_dir.mkdir()
    for f in sorted((PROJECT_ROOT / "templates").glob("*.json")):
        shutil.copy2(f, tpl_dir / f.name)

    from app import app

    with TestClient(app) as c:
        yield c