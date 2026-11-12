from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from . import config, db, storage
from .models import Document
from .routes import api, dashboard, documents, export, people_page, progress_page, review, workdays
from .settings import get_settings

log = logging.getLogger("steuer.web")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    st = get_settings()
    if st.auth_user and not st.auth_password:
        raise RuntimeError("STEUER_USER is set but STEUER_PASSWORD is empty.")
    db.configure()
    with db.session_scope() as s:
        has_docs = (s.scalar(select(func.count(Document.id))) or 0) > 0
    storage.init_marker(has_docs)
    for problem in config.validate():
        log.error("config problem: %s", problem)
    yield


app = FastAPI(title="Steuer Hub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """CSRF guard: browsers attach Basic-auth credentials to cross-site form posts, so refuse them."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-origin request blocked", status_code=403)
    return await call_next(request)


@app.get("/healthz", include_in_schema=False)
def healthz():
    # Docker calls this every minute. Reusing the NAS check for 15 min (the inbox-scan rhythm) lets the NAS disks
    # spin down; pages and uploads still check the NAS on every request.
    ok, msg = storage.nas_status_cached(max_age=900)
    return PlainTextResponse("ok" if ok else f"nas: {msg}", status_code=200 if ok else 503)


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
for module in (dashboard, documents, workdays, people_page, review, export, progress_page, api):
    app.include_router(module.router)
