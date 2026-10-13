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
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from . import desktop_session
from fastapi.staticfiles import StaticFiles

from harness import config
from . import database
from .routers import cancel, chat, folders, knowledge, papers, related, reviews, rules, search, skills, translations
from .routers import settings as settings_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ArxivReader")

STATIC_DIR = os.path.join(config.ROOT_DIR, "static")


class NoCacheStaticFiles(StaticFiles):
    """静态资源禁用强制缓存（保留 ETag/304 协商）。

    本地应用的特点是“代码改完就刷新看效果”，若被浏览器强缓存，
    用户会在旧样式/旧脚本上排查半天；no-cache 让每次都可能问一下服务端。
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    from knowledge.review import recover_interrupted
    from knowledge.related import recover_interrupted as recover_related
    from knowledge.pipeline import recover_interrupted as recover_pipeline
    with database.SessionLocal() as db:
        recover_interrupted(db)
        recover_related(db)
        recover_pipeline(db)
    logger.info("数据库就绪：%s", config.get_db_path())
    logger.info("PDF 存放目录：%s", config.get_pdf_dir())
    # Agent Harness 自检：启动时把已注册能力打出来，便于确认“新增技能/工具”是否生效
    from harness import skills as harness_skills, tools as harness_tools
    from knowledge import STEPS as knowledge_steps
    logger.info("Agent Harness：技能=%s；工具=%s",
                ",".join(harness_skills.skill_names()), ",".join(harness_tools.tool_names()))
    logger.info("知识网络 pipeline：%s", " -> ".join(knowledge_steps))
    from knowledge import SEARCH_STAGES as search_stages
    logger.info("检索 pipeline：%s", " -> ".join(search_stages))
    from knowledge import RELATED_STAGES as related_stages
    logger.info("关联检索 pipeline：%s", " -> ".join(related_stages))
    yield


app = FastAPI(title="ArxivReader", version="0.2.0", lifespan=lifespan,
              docs_url=None if desktop_session.session else "/docs",
              redoc_url=None if desktop_session.session else "/redoc",
              openapi_url=None if desktop_session.session else "/openapi.json")
app.add_middleware(desktop_session.DesktopSessionMiddleware)


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    from .messages import error_detail
    detail = error_detail(exc.detail, exc.status_code)
    return JSONResponse({"detail": detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse({"detail": {"code": "error.422", "params": {}}}, status_code=422)

# REST 路由（先于静态挂载注册，确保 /api/* 优先匹配）
app.include_router(cancel.router)
app.include_router(folders.router)
app.include_router(papers.router)
app.include_router(rules.router)
app.include_router(settings_router.router)
app.include_router(chat.router)
app.include_router(skills.router)
app.include_router(knowledge.router)
app.include_router(search.router)
app.include_router(related.router)
app.include_router(reviews.router)
app.include_router(translations.router)


@app.get("/api/health", tags=["meta"])
def health():
    return {"status": "ok", "version": app.version}


# 静态前端（SPA）：html=True 使 "/" 自动返回 index.html
app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8765, reload=False)
