"""FastAPI 앱: JSON API + 정적 화면(PWA) + 썸네일 (DESIGN.md 6장).

- /api/*      JSON API (토큰 필요, routes/api.py)
- /thumbs/*   data/thumbs 의 썸네일 (토큰 필요)
- 그 밖      static/ 의 화면 파일. 화면 껍데기에는 데이터가 없어서 토큰 없이 받는다
              (토큰 입력 화면·서비스 워커·manifest 가 로그인 전에도 떠야 하므로).
"""

import asyncio
import logging
import re
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from reelbox import db
from reelbox.config import Settings
from reelbox.purge import purge
from reelbox.search.embed import Embedder
from reelbox.web.auth import COOKIE_MAX_AGE, COOKIE_NAME, is_https, require_token, token_matches
from reelbox.web.routes import api

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PURGE_INTERVAL_SEC = 24 * 60 * 60
_THUMB_NAME_RE = re.compile(r"^[\w.-]+\.(webp|jpg|jpeg|png)$")


class EmbedderHolder:
    """임베딩 모델을 백그라운드에서 불러온다. 다 불러오기 전에는 get() 이 None → 키워드 검색만.

    bge-m3 는 불러오는 데 CPU 에서 30초쯤 걸려서, 서버가 그동안 멈춰 있지 않게 한다.
    """

    def __init__(self, factory: Callable[[], Embedder] | None) -> None:
        self._factory = factory
        self._embedder: Embedder | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._factory is None:
            return
        threading.Thread(target=self._load, name="embedder-load", daemon=True).start()

    def _load(self) -> None:
        assert self._factory is not None
        try:
            embedder = self._factory()
            embedder.encode(["warm up"])  # 실제로 모델을 불러오게 한 번 돌린다
        except Exception:
            logger.exception("임베딩 모델을 못 불러옴 — 키워드 검색만 합니다")
            return
        with self._lock:
            self._embedder = embedder
        logger.info("임베딩 모델 준비됨 — 의미 검색 사용")

    def get(self) -> Embedder | None:
        with self._lock:
            return self._embedder


def _purge_once(settings: Settings) -> None:
    conn = db.connect(settings.db_path)
    try:
        purge(conn, settings, now=datetime.now(UTC))
    finally:
        conn.close()


async def _purge_daily(settings: Settings) -> None:
    """서버가 켜져 있는 동안 하루 한 번 휴지통 정리 (DESIGN.md 4장)."""
    while True:
        try:
            await asyncio.to_thread(_purge_once, settings)
        except sqlite3.Error:
            logger.exception("자동 휴지통 정리 실패")
        await asyncio.sleep(PURGE_INTERVAL_SEC)


class LoginIn(BaseModel):
    token: str


def create_app(
    settings: Settings,
    *,
    embedder_factory: Callable[[], Embedder] | None = None,
    auto_purge: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """앱을 만든다. 테스트에서는 embedder_factory 에 가짜를, auto_purge=False 를 넘긴다."""
    if settings.token is None or not settings.token.get_secret_value().strip():
        raise ValueError("REELBOX_TOKEN 이 비어 있음 — .env 에 긴 무작위 토큰을 넣어주세요")

    holder = EmbedderHolder(embedder_factory)

    # lifespan: 서버가 켜질 때 한 번 앞부분, 꺼질 때 yield 뒷부분이 돈다 (Go 의 defer 와 비슷)
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        holder.start()
        task = asyncio.create_task(_purge_daily(settings)) if auto_purge else None
        yield
        if task is not None:
            task.cancel()

    app = FastAPI(title="Reelbox", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.embedder = holder
    app.state.now = now

    @app.post("/api/login")
    def login(request: Request, response: Response, body: LoginIn) -> dict[str, bool]:
        if not token_matches(settings, body.token.strip()):
            raise HTTPException(401, "토큰이 맞지 않습니다")
        response.set_cookie(
            COOKIE_NAME,
            body.token.strip(),
            max_age=COOKIE_MAX_AGE,
            httponly=True,  # 화면 JS 가 읽을 수 없게
            secure=is_https(request),
            samesite="lax",
        )
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME)
        return {"ok": True}

    app.include_router(api.router)

    @app.get("/thumbs/{name}", dependencies=[Depends(require_token)])
    def thumb(name: str) -> FileResponse:
        if not _THUMB_NAME_RE.match(name):
            raise HTTPException(404)
        path = settings.thumbs_dir / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=604800"})

    # 맨 마지막에 붙여야 /api 등 위 경로가 먼저 잡힌다. html=True: / 에서 index.html 을 준다.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
