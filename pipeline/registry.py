"""
模板注册表
管理所有 API 模板的加载、查询、验证
支持从 config.yaml 注入 {{placeholder}} 值
"""
import json
from pathlib import Path

from pipeline.log import get_logger
from pipeline.messages import Msg
log = get_logger("registry")


class TemplateRegistry:
    """模板注册表：加载 / 查询 / 验证 / 配置注入"""

    def __init__(self, template_dir: str = "templates", config: dict = None):
        self.template_dir = Path(template_dir)
        self.config = config or {}
        self.templates: dict[str, dict] = {}
        self._load_all()

    def _load_all(self):
        """加载目录下所有 .json 模板"""
        if not self.template_dir.exists():
            log.warning(Msg.REG_DIR_MISSING.format(dir=self.template_dir))
            return

        for f in sorted(self.template_dir.glob("*.json")):
            try:
                with open(f, encoding="utf-8") as fp:
                    tpl = json.load(fp)
                tid = tpl.get("id", f.stem)
                self.templates[tid] = tpl
                log.info(Msg.REG_REGISTERED.format(tid=tid, name=tpl.get("name", "?")))
            except Exception as e:
                log.error(Msg.REG_LOAD_FAIL.format(file=f.name, err=e))

    def _resolve_placeholders(self, obj, config: dict):
        """递归替换 {{section.key}} 占位符为 config 中的实际值"""
        if isinstance(obj, str):
            if obj.startswith("{{") and obj.endswith("}}"):
                # 解析 {{section.key}} → config[section][key]
                path = obj[2:-2].strip()
                parts = path.split(".")
                val = config
                for p in parts:
                    if isinstance(val, dict) and p in val:
                        val = val[p]
                    else:
                        return obj  # 未找到，保留原始值
                return val
            return obj
        elif isinstance(obj, dict):
            return {k: self._resolve_placeholders(v, config) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._resolve_placeholders(item, config) for item in obj]
        return obj

    def get(self, template_id: str) -> dict:
        """获取模板（已解析占位符）"""
        if template_id not in self.templates:
            raise KeyError(f"模板不存在: {template_id}")
        tpl = self.templates[template_id]
        if self.config:
            return self._resolve_placeholders(tpl, self.config)
        return tpl

    def get_raw(self, template_id: str) -> dict:
        """获取原始模板（不解析占位符）"""
        if template_id not in self.templates:
            raise KeyError(f"模板不存在: {template_id}")
        return self.templates[template_id]

    def get_workflow(self, template_id: str) -> dict:
        """获取 ComfyUI 工作流部分（已解析）"""
        tpl = self.get(template_id)
        wf = tpl.get("workflow")
        if not wf:
            raise ValueError(f"模板 {template_id} 无 workflow（workflow_type={tpl.get('workflow_type')})")
        return wf

    def find_best(self, asset_type: str = "", keywords: list[str] = None,
                  scene_desc: str = "") -> str:
        """
        最佳模板匹配

        优先级：asset_type 精确匹配(+10) > 关键词命中(+1) > comfyui 破平(+0.5)
        """
        if keywords is None:
            keywords = []

        all_kw = keywords.copy()
        if scene_desc:
            all_kw.extend(self._extract_keywords(scene_desc))

        scored = []
        for tid, tpl in self.templates.items():
            rules = tpl.get("match_rules", {})
            valid_types = rules.get("asset_type", [])
            tpl_kw = rules.get("keywords", [])
            workflow_type = tpl.get("workflow_type", "comfyui")

            type_score = 10 if (asset_type and asset_type in valid_types) else 0
            kw_score = sum(1 for kw in all_kw if kw in tpl_kw)
            bonus = 0.5 if workflow_type == "comfyui" else 0

            scored.append({"tid": tid, "score": type_score + kw_score + bonus})

        scored.sort(key=lambda x: -x["score"])
        return scored[0]["tid"]

    def _extract_keywords(self, text: str) -> list[str]:
        """从场景描述提取关键词"""
        kw_map = {
            "分屏": ["分屏"], "对比": ["对比"], "并排": ["并排"],
            "文字": ["文字"], "动画": ["动画"], "模块": ["模块", "文字"],
            "字幕": ["字幕"], "列表": ["列表", "文字"],
        }
        result = []
        for trigger, extras in kw_map.items():
            if trigger in text:
                result.extend(extras)
        return result

    def list_all(self) -> list[dict]:
        """列出所有注册的模板"""
        return [
            {
                "id": tid,
                "name": tpl.get("name", ""),
                "category": tpl.get("category", ""),
                "description": tpl.get("description", ""),
                "workflow_type": tpl.get("workflow_type", "comfyui"),
                "match_rules": tpl.get("match_rules", {}),
                "usage": tpl.get("usage", {}),
            }
            for tid, tpl in self.templates.items()
        ]

    def summary(self) -> str:
        lines = [f"\n  [Registry] 共注册 {len(self.templates)} 个模板:"]
        for tid, tpl in self.templates.items():
            cat = tpl.get("category", "?")
            wtype = tpl.get("workflow_type", "comfyui")
            name = tpl.get("name", "?")
            lines.append(f"        [{cat:12s}] {tid:<30s} ({wtype}) {name}")
        return "\n".join(lines)