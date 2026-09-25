"""FastAPI 应用入口：挂载 REST 路由与静态前端，启动时初始化数据库。

运行（在 paper_reader/ 目录下）：
    python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8765
或直接：
    python3 -m app.main
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import config_loader, database
from .routers import chat, folders, papers, rules
from .routers import settings as settings_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ArxivReader")

STATIC_DIR = os.path.join(config_loader.ROOT_DIR, "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    logger.info("数据库就绪：%s", config_loader.get_db_path())
    logger.info("PDF 存放目录：%s", config_loader.get_pdf_dir())
    yield


app = FastAPI(title="ArxivReader", version="0.1.0", lifespan=lifespan)

# 前端与后端同源；开启宽松 CORS 仅为方便本地调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST 路由（先于静态挂载注册，确保 /api/* 优先匹配）
app.include_router(folders.router)
app.include_router(papers.router)
app.include_router(rules.router)
app.include_router(settings_router.router)
app.include_router(chat.router)


@app.get("/api/health", tags=["meta"])
def health():
    return {"status": "ok", "version": app.version}


# 静态前端（SPA）：html=True 使 "/" 自动返回 index.html
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8765, reload=False)
