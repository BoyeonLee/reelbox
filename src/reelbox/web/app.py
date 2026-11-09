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
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from reelbox import db
from reelbox.config import Settings
from reelbox.purge import purge
from reelbox.search.embed import Embedder
from reelbox.web.auth import COOKIE_MAX_AGE, COOKIE_NAME, is_https, require_token, token_matches
from reelbox.web.routes import api
from reelbox.worker import EmbedderGetter, Worker

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


class ShellFiles(StaticFiles):
    """화면 파일(HTML·JS·manifest·서비스 워커). 매번 바뀌었는지 서버에 물어보게 한다.

    Cache-Control 이 없으면 Safari 가 "오래된 파일이니 한동안 그냥 써도 된다"고 스스로 판단해서,
    새 index.html 과 옛 app.js 가 섞일 수 있다 (Phase 6 에서 실제로 겪음: 새 버튼이 눌리지 않음).
    no-cache 는 "저장은 하되 쓰기 전에 확인" 이라서, 안 바뀌었으면 304 로 짧게 끝난다.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class LoginIn(BaseModel):
    token: str


def create_app(
    settings: Settings,
    *,
    embedder_factory: Callable[[], Embedder] | None = None,
    auto_purge: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    make_worker: Callable[[EmbedderGetter], Worker] | None = None,
) -> FastAPI:
    """앱을 만든다. 테스트에서는 embedder_factory 에 가짜를, auto_purge=False 를 넘긴다.

    make_worker: 서버가 불러온 임베딩 모델을 꺼내는 함수를 받아 워커를 만든다 (serve 가 넘김).
    None 이면 공유 항목 자동 처리를 하지 않는다.
    """
    if settings.token is None or not settings.token.get_secret_value().strip():
        raise ValueError("REELBOX_TOKEN 이 비어 있음 — .env 에 긴 무작위 토큰을 넣어주세요")

    holder = EmbedderHolder(embedder_factory)
    worker = make_worker(holder.get) if make_worker is not None else None

    # lifespan: 서버가 켜질 때 한 번 앞부분, 꺼질 때 yield 뒷부분이 돈다 (Go 의 defer 와 비슷)
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        holder.start()
        if worker is not None:
            worker.start()
        task = asyncio.create_task(_purge_daily(settings)) if auto_purge else None
        yield
        if task is not None:
            task.cancel()
        if worker is not None:
            worker.stop()

    app = FastAPI(title="Reelbox", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.embedder = holder
    app.state.worker = worker
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

    # 오류 응답에도 message 를 넣는다. 아이폰 단축어는 응답의 message 를 알림으로 띄우므로,
    # 거절됐을 때도 이유가 보이게 한다. 화면(data.js)은 지금처럼 detail 을 읽는다.
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "요청을 처리하지 못했어요"
        if exc.status_code == 401:
            message = (
                "토큰이 맞지 않아요. 단축어 헤더가 'Authorization: Bearer 토큰' 인지 확인하세요"
            )
            logger.warning("토큰 없음·틀림: %s %s", request.method, request.url.path)
        return JSONResponse(
            {"detail": exc.detail, "message": message},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = jsonable_encoder(exc.errors())
        # 값 자체(공유한 링크·글)는 남기지 않고, 어느 칸이 어떤 이유로 틀렸는지만 남긴다
        summary = "; ".join(
            f"{'.'.join(map(str, e.get('loc', [])))}: {e.get('msg')}" for e in errors
        )
        logger.warning("요청 형식 오류: %s %s — %s", request.method, request.url.path, summary)
        return JSONResponse(
            {
                "detail": errors,
                "message": f"보낸 내용을 읽지 못했어요 ({summary}). 단축어 본문이 JSON 이고 "
                "키가 url 인지 확인하세요",
            },
            status_code=422,
        )

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
    app.mount("/", ShellFiles(directory=STATIC_DIR, html=True), name="static")
    return app
