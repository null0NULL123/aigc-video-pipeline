"""
aigc-video Web 管理面板 — FastAPI 主应用
"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from web import settings
from web.routers import config_api, tables, pipeline, videos, system, media, assets

app = FastAPI(title="aigc-video 管理面板")

# 挂载路由
app.include_router(config_api.router, prefix="/api")
app.include_router(tables.router, prefix="/api")
app.include_router(pipeline.router, prefix="/api")
app.include_router(videos.router, prefix="/api")
app.include_router(system.router, prefix="/api")
app.include_router(media.router, prefix="/api")
app.include_router(assets.router, prefix="/api")

# 静态文件
app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")


@app.get("/")
async def index():
    # 禁止缓存 index.html，保证前端改动即时生效
    return FileResponse(
        str(settings.STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
