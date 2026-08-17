"""cli.ensure_dirs 输出目录解析测试

守护 config.yaml 的 task.output_structure 分类目录结构：
output/{shots,audio,subs,merged,final,logs}/{project}/
"""
import yaml
from pathlib import Path

import pytest


@pytest.fixture
def cfg_factory(tmp_path):
    """生成临时 config，避免污染用户真实 config.yaml"""
    def _make(project: str, structure: dict | None = None):
        return {
            "_batch_id": project,
            "task": {
                "batch_prefix": "batch",
                "output_structure": structure or {
                    "shots":  "output/shots/{project}",
                    "audio":  "output/audio/{project}",
                    "subs":   "output/subs/{project}",
                    "merged": "output/merged/{project}",
                    "final":  "output/final/{project}",
                    "logs":   "output/logs/{project}",
                },
            },
        }
    return _make


def test_ensure_dirs_resolves_all_six_categories(cfg_factory, tmp_path, monkeypatch):
    """{project} 占位符正确解析到 6 个分类目录"""
    monkeypatch.chdir(tmp_path)
    from cli import ensure_dirs

    cfg = cfg_factory("demo_2026")
    ensure_dirs(cfg)

    expected = {
        "shots_dir":  "output/shots/demo_2026",
        "audio_dir":  "output/audio/demo_2026",
        "subs_dir":   "output/subs/demo_2026",
        "merged_dir": "output/merged/demo_2026",
        "final_dir":  "output/final/demo_2026",
        "logs_dir":   "output/logs/demo_2026",
    }
    for key, want in expected.items():
        assert cfg["output"][key] == want, f"{key}: got {cfg['output'][key]!r}, want {want!r}"
        assert (tmp_path / want).is_dir(), f"{want} not created"


def test_ensure_dirs_matches_config_yaml_structure(cfg_factory, tmp_path, monkeypatch):
    """真实 config.yaml 的 task.output_structure 能被解析（守护同步）"""
    import yaml
    real_cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    assert "task" in real_cfg and "output_structure" in real_cfg["task"], \
        "config.yaml 缺 task.output_structure，本次改动守护失败"

    monkeypatch.chdir(tmp_path)
    from cli import ensure_dirs

    cfg = {
        "_batch_id": "batch_20260817_999999",
        "task": real_cfg["task"],
    }
    ensure_dirs(cfg)

    for k in ("shots_dir","audio_dir","subs_dir","merged_dir","final_dir","logs_dir"):
        p = tmp_path / cfg["output"][k]
        assert p.is_dir(), f"{k} ({p}) not created"
        assert "batch_20260817_999999" in str(p)


def test_ensure_dirs_supports_legacy_batch_id_placeholder(cfg_factory, tmp_path, monkeypatch):
    """{batch_id} 旧占位符仍可用（兼容旧 config）"""
    monkeypatch.chdir(tmp_path)
    from cli import ensure_dirs

    cfg = cfg_factory("demo_legacy", structure={
        "shots": "output/{batch_id}/shots",
        "audio": "output/{batch_id}/audio",
    })
    ensure_dirs(cfg)
    assert cfg["output"]["shots_dir"] == "output/demo_legacy/shots"
    assert cfg["output"]["audio_dir"] == "output/demo_legacy/audio"
    assert (tmp_path / "output/demo_legacy/shots").is_dir()
    assert (tmp_path / "output/demo_legacy/audio").is_dir()


def test_ensure_dirs_no_structure_does_not_overwrite(cfg_factory, tmp_path, monkeypatch):
    """task.output_structure 未配置时，保持 config['output'] 原值不动"""
    monkeypatch.chdir(tmp_path)
    from cli import ensure_dirs

    cfg = {
        "_batch_id": "demo_x",
        "task": {},  # no output_structure
        "output": {"shots_dir": str(tmp_path / "custom" / "path" / "shots")},
    }
    ensure_dirs(cfg)
    assert cfg["output"]["shots_dir"] == str(tmp_path / "custom" / "path" / "shots")
    assert (tmp_path / "custom" / "path" / "shots").is_dir()
