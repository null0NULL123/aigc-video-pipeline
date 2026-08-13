"""
配置管理 API
- GET  /api/config          获取完整配置
- PUT  /api/config          保存配置（自动备份旧文件）
- POST /api/config/reset    从备份恢复
- GET  /api/templates        模板列表
- GET  /api/templates/{id}   模板详情
- POST /api/templates/import 导入新模板
"""
import json
import shutil
import yaml
from datetime import datetime
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse

from web import settings

router = APIRouter(tags=["config"])


# ── 配置读写 ──────────────────────────────────────────────

@router.get("/config")
async def get_config():
    """返回 config.yaml 完整内容（嵌套 dict）"""
    if not settings.CONFIG_PATH.exists():
        raise HTTPException(404, "config.yaml 不存在")
    with open(settings.CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data


@router.put("/config")
async def save_config(body: dict):
    """保存配置：先备份旧文件，再写入新内容"""
    # 备份
    backup = None
    if settings.CONFIG_PATH.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = settings.CONFIG_PATH.with_name(f"config.yaml.bak.{ts}")
        shutil.copy2(settings.CONFIG_PATH, backup)
        # 同时维护一个固定的 .bak 副本
        shutil.copy2(settings.CONFIG_PATH, settings.BACKUP_PATH)

    # 写入
    with open(settings.CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(body, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    return {"ok": True, "message": "配置已保存", "backup": str(backup) if backup else None}


@router.post("/config/reset")
async def reset_config():
    """从 config.yaml.bak 恢复"""
    if not settings.BACKUP_PATH.exists():
        raise HTTPException(404, "没有备份文件可恢复")
    shutil.copy2(settings.BACKUP_PATH, settings.CONFIG_PATH)
    with open(settings.CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {"ok": True, "message": "已从备份恢复", "config": data}


# ── 模板管理 ──────────────────────────────────────────────

@router.get("/templates")
async def list_templates():
    """列出所有模板（ID + 名称 + 描述）"""
    if not settings.TEMPLATE_DIR.exists():
        return []
    result = []
    for f in sorted(settings.TEMPLATE_DIR.glob("*.json")):
        with open(f, encoding="utf-8") as fp:
            tpl = json.load(fp)
        result.append({
            "id": tpl.get("id", f.stem),
            "name": tpl.get("name", ""),
            "category": tpl.get("category", ""),
            "description": tpl.get("description", ""),
            "workflow_type": tpl.get("workflow_type", "comfyui"),
            "file": f.name,
        })
    return result


@router.get("/templates/{template_id}")
async def get_template(template_id: str):
    """获取单个模板完整内容"""
    for f in settings.TEMPLATE_DIR.glob("*.json"):
        with open(f, encoding="utf-8") as fp:
            tpl = json.load(fp)
        if tpl.get("id", f.stem) == template_id:
            return tpl
    raise HTTPException(404, f"模板不存在: {template_id}")


@router.post("/templates/import")
async def import_template(file: UploadFile = File(...)):
    """导入新模板 JSON 文件"""
    if not file.filename.endswith(".json"):
        raise HTTPException(400, "只支持 .json 文件")
    settings.TEMPLATE_DIR.mkdir(exist_ok=True)
    dest = settings.TEMPLATE_DIR / file.filename
    content = await file.read()
    # 验证是合法 JSON
    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"JSON 格式错误: {e}")
    with open(dest, "wb") as f:
        f.write(content)
    return {"ok": True, "message": f"模板 {file.filename} 已导入"}
