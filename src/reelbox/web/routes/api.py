"""JSON API. 화면(static/data.js)과 아이폰 단축어가 쓴다. 모든 경로는 토큰이 필요하다.

FastAPI 메모 (Go 와 비교):
- @router.get(...) 는 Go 의 mux.HandleFunc 와 비슷하다. 함수 인자의 타입 힌트를 보고
  쿼리 문자열·JSON 본문을 자동으로 읽고 검증한다 (틀리면 422 응답).
- Depends(...) 는 핸들러 앞에 끼우는 미들웨어/주입. 여기서는 DB 연결과 토큰 확인에 쓴다.
- def(동기) 핸들러는 FastAPI 가 스레드 풀에서 돌려서, sqlite 처럼 막히는 호출을 써도 된다.
"""

import logging
import re
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from reelbox import db
from reelbox.purge import TRASH_DAYS, purge
from reelbox.search import query as searcher
from reelbox.urls import parse_url
from reelbox.web.auth import require_token
from reelbox.worker import SHARE_SOURCE, Worker, WorkerState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """요청마다 연결을 열고 닫는다 (sqlite 연결은 스레드끼리 나눠 쓰지 않는다)."""
    conn = db.connect(request.app.state.settings.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _now(request: Request) -> datetime:
    return request.app.state.now()


# --- 응답 모델 ---


def thumb_url(thumbnail_path: str | None) -> str | None:
    """data/thumbs/ 안의 파일 이름만 주소로 내보낸다 (스냅샷에서도 같은 모양으로 쓴다)."""
    return f"/thumbs/{Path(thumbnail_path).name}" if thumbnail_path else None


class Card(BaseModel):
    id: int
    platform: str
    url: str
    title: str | None
    author: str | None
    summary: str | None
    topic_id: int | None
    topic: str | None
    saved_at: str  # 저장 시각이 없으면 처음 가져온 시각
    duration_sec: float | None
    thumb: str | None
    media_type: str | None
    status: str
    error: str | None
    hidden: bool
    deleted_at: str | None
    purge_at: str | None  # 휴지통 항목이 영구 정리되는 시각

    @classmethod
    def of(cls, c: db.ItemCard) -> "Card":
        purge_at = None
        if c.deleted_at:
            deleted = datetime.fromisoformat(c.deleted_at)
            purge_at = db.to_iso(deleted + timedelta(days=TRASH_DAYS))
        return cls(
            id=c.id,
            platform=c.platform,
            url=c.url,
            title=c.title,
            author=c.author,
            summary=c.summary_ko,
            topic_id=c.topic_id,
            topic=c.topic,
            saved_at=c.saved_at or c.created_at,
            duration_sec=c.duration_sec,
            thumb=thumb_url(c.thumbnail_path),
            media_type=c.media_type,
            status=c.status,
            error=c.error,
            hidden=c.hidden_at is not None,
            deleted_at=c.deleted_at,
            purge_at=purge_at,
        )


class Detail(Card):
    description: str | None
    key_points: list[str]
    tags: list[str]
    transcript_source: str | None
    transcript: str | None
    similar: list[Card]


class ItemPage(BaseModel):
    items: list[Card]
    next_offset: int | None  # 더 불러올 것이 없으면 None


class TopicOut(BaseModel):
    id: int
    name: str
    description: str | None
    count: int
    thumbs: list[str]


class HomeOut(BaseModel):
    total: int
    topics: list[TopicOut]
    recent: list[Card]


class SearchOut(ItemPage):
    semantic: bool  # 의미 검색까지 했는지 (모델을 불러오는 중이면 False)


class StatusOut(BaseModel):
    counts: dict[str, int]
    total: int
    hidden: int
    trash: int
    failures: list[db.FailedItem]


def _page(cards: list[db.ItemCard], limit: int, offset: int) -> ItemPage:
    next_offset = offset + len(cards) if len(cards) == limit else None
    return ItemPage(items=[Card.of(c) for c in cards], next_offset=next_offset)


# --- 조회 ---

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


@router.get("/home")
def home(conn: Conn, recent: Limit = 10) -> HomeOut:
    topics = [
        TopicOut(
            id=t.id,
            name=t.name,
            description=t.description,
            count=t.items,
            thumbs=[u for p in t.thumbnail_paths if (u := thumb_url(p))],
        )
        for t in db.topic_cards(conn)
    ]
    return HomeOut(
        total=db.count_listable(conn),
        topics=topics,
        recent=[Card.of(c) for c in db.list_items(conn, limit=recent)],
    )


@router.get("/topics/{topic_id}")
def topic(conn: Conn, topic_id: int) -> TopicOut:
    t = db.get_topic(conn, topic_id)
    if t is None:
        raise HTTPException(404, "주제를 찾을 수 없습니다")
    return TopicOut(id=t.id, name=t.name, description=t.description, count=t.items, thumbs=[])


@router.get("/items")
def items(
    conn: Conn,
    topic_id: int | None = None,
    sort: Literal["recent", "oldest"] = "recent",
    limit: Limit = 30,
    offset: Offset = 0,
) -> ItemPage:
    cards = db.list_items(
        conn, topic_id=topic_id, oldest_first=sort == "oldest", limit=limit, offset=offset
    )
    return _page(cards, limit, offset)


@router.get("/items/{item_id}")
def item(conn: Conn, item_id: int) -> Detail:
    d = db.get_item_detail(conn, item_id)
    if d is None:
        raise HTTPException(404, "영상을 찾을 수 없습니다")
    return Detail(
        **Card.of(d).model_dump(),
        description=d.description,
        key_points=d.key_points,
        tags=d.tags,
        transcript_source=d.transcript_source,
        transcript=d.transcript_text,
        similar=[Card.of(c) for c in d.similar],
    )


@router.get("/search")
def search(
    request: Request,
    conn: Conn,
    q: str = "",
    platform: Literal["instagram", "youtube"] | None = None,
    topic: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> SearchOut:
    embedder = request.app.state.embedder.get()
    filters = searcher.SearchFilters(platform=platform, topic=topic, since=since, until=until)
    hits = searcher.search(conn, q, embedder=embedder, filters=filters, limit=limit, offset=offset)
    page = _page(db.cards_by_ids(conn, [h.id for h in hits]), limit, offset)
    return SearchOut(**page.model_dump(), semantic=embedder is not None)


@router.get("/status")
def status(conn: Conn) -> StatusOut:
    counts = db.status_counts(conn)
    meta = db.meta_counts(conn)
    return StatusOut(
        counts=counts,
        total=sum(n for s, n in counts.items() if s != "deleted"),
        hidden=meta["hidden"],
        trash=meta["trash"],
        failures=db.recent_failures(conn, limit=20),
    )


@router.get("/hidden")
def hidden(conn: Conn) -> list[Card]:
    return [Card.of(c) for c in db.list_hidden(conn)]


@router.get("/trash")
def trash(conn: Conn) -> list[Card]:
    return [Card.of(c) for c in db.list_trash(conn)]


# --- 숨기기·삭제·복원 ---


class HiddenPatch(BaseModel):
    hidden: bool


class Changed(BaseModel):
    ids: list[int]  # 실제로 바뀐 항목


def _one(changed: list[int]) -> Changed:
    if not changed:
        raise HTTPException(404, "영상을 찾을 수 없습니다")
    return Changed(ids=changed)


@router.patch("/items/{item_id}")
def patch_item(request: Request, conn: Conn, item_id: int, body: HiddenPatch) -> Changed:
    return _one(db.set_hidden(conn, [item_id], body.hidden, now=_now(request)))


@router.delete("/items/{item_id}")
def delete_item(request: Request, conn: Conn, item_id: int) -> Changed:
    return _one(db.trash_items(conn, [item_id], now=_now(request)))


@router.post("/items/{item_id}/restore")
def restore_item(conn: Conn, item_id: int) -> Changed:
    return _one(db.restore_items(conn, [item_id]))


class BulkIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: Literal["hide", "unhide", "delete", "restore"]


@router.post("/items/bulk")
def bulk(request: Request, conn: Conn, body: BulkIn) -> Changed:
    now = _now(request)
    match body.action:
        case "hide":
            changed = db.set_hidden(conn, body.ids, True, now=now)
        case "unhide":
            changed = db.set_hidden(conn, body.ids, False, now=now)
        case "delete":
            changed = db.trash_items(conn, body.ids, now=now)
        case "restore":
            changed = db.restore_items(conn, body.ids)
    return Changed(ids=changed)


@router.post("/trash/empty")
def empty_trash(request: Request, conn: Conn) -> Changed:
    """휴지통 비우기: 30일을 기다리지 않고 지금 전부 영구 정리한다."""
    ids = purge(conn, request.app.state.settings, now=_now(request), days=None)
    return Changed(ids=ids)


# --- 추가 (링크 붙여넣기·공유) ---


class AddIn(BaseModel):
    # 링크 하나. 공유 시트가 캡션 등을 붙여 보내도 받을 수 있게 넉넉히
    url: str = Field(min_length=1, max_length=10000)

    @field_validator("url", mode="before")
    @classmethod
    def _join_list(cls, value: object) -> object:
        """아이폰 단축어는 공유 항목이 여러 개(링크 + 제목 글 등)면 목록으로 보낸다.

        목록이면 한 줄씩 이어 붙이고, 그 안에서 인식되는 첫 링크를 add() 가 고른다.
        (mode="before": 타입 검사 전에 먼저 돈다. Go 의 UnmarshalJSON 을 직접 구현하는 것과 비슷)
        """
        if isinstance(value, list):
            return "\n".join(str(v) for v in value if v is not None)
        return value

    @model_validator(mode="before")
    @classmethod
    def _url_anywhere(cls, data: object) -> object:
        """단축어에서 키 칸을 비워 두거나 URL 처럼 대소문자만 다르게 적어도 받는다.

        실제로 겪음: 키 칸이 비고 값 칸에 "url" 글자와 단축어 입력이 함께 들어가
        {"": "url https://…"} 로 왔다. 그런 값들을 이어 붙여 그 안에서 링크를 찾는다.
        전혀 다른 키(예: link)는 그대로 422 로 돌려보내 단축어 알림에 이유가 뜨게 한다.
        """
        if not isinstance(data, dict) or "url" in data:
            return data
        loose = [
            v for k, v in data.items() if isinstance(k, str) and k.strip().lower() in ("", "url")
        ]
        if not loose:
            return data
        parts: list[str] = []
        for v in loose:
            parts.extend(str(x) for x in (v if isinstance(v, list) else [v]) if x is not None)
        return {"url": "\n".join(parts)}


class AddOut(BaseModel):
    result: Literal["added", "duplicate", "restored", "deleted", "unrecognized"]
    message: str
    item: Card | None


_ADD_MESSAGES = {
    "added": "추가했어요. 곧 처리돼요",
    "duplicate": "이미 저장된 영상이에요",
    "restored": "휴지통에 있던 영상을 복원했어요",
    "deleted": "이미 삭제한 영상이라 건너뛰었어요",
    "unrecognized": "인스타그램 릴스나 유튜브 쇼츠 링크가 아니에요",
}


_URL_IN_TEXT = re.compile(r"https?://\S+")


def _pick_url(text: str) -> str:
    """공유 시트는 링크 앞뒤에 글(캡션 등)을 붙여 보내기도 한다 → 인식되는 첫 링크를 고른다."""
    text = text.strip()
    if parse_url(text) is not None:
        return text
    for m in _URL_IN_TEXT.finditer(text):
        if parse_url(m.group(0)) is not None:
            return m.group(0)
    return text


def _worker(request: Request) -> Worker | None:
    return request.app.state.worker


@router.post("/items")
def add(request: Request, conn: Conn, body: AddIn) -> AddOut:
    """링크 하나 추가 (추가 화면·아이폰 단축어). 처리는 백그라운드 워커가 한다.

    휴지통에 있는 영상을 다시 공유하면 복원하고, 영구 삭제한 영상은 건너뛴다 (DESIGN.md 4장).
    """
    parsed = parse_url(_pick_url(body.url))
    if parsed is None:
        logger.warning("추가: 인식할 수 없는 링크 (%d자)", len(body.url))
        return AddOut(result="unrecognized", message=_ADD_MESSAGES["unrecognized"], item=None)

    result: Literal["added", "duplicate", "restored", "deleted"]
    existing = db.find_item_id(conn, parsed.platform, parsed.source_id)
    if existing is not None and db.is_trashed(conn, existing):
        db.restore_items(conn, [existing])
        result = "restored"
    else:
        outcome = db.add_item(conn, parsed.url, saved_at=None, source=SHARE_SOURCE)
        conn.commit()
        result = outcome.value  # type: ignore[assignment]

    worker = _worker(request)
    if worker is not None and result in ("added", "restored"):
        worker.notify()

    item_id = db.find_item_id(conn, parsed.platform, parsed.source_id)
    cards = db.cards_by_ids(conn, [item_id]) if item_id is not None and result != "deleted" else []
    return AddOut(
        result=result,
        message=_ADD_MESSAGES[result],
        item=Card.of(cards[0]) if cards else None,
    )


@router.get("/adds")
def adds(conn: Conn) -> list[Card]:
    """추가 화면: 최근에 붙여넣기·공유로 들어온 항목과 처리 상태."""
    return [Card.of(c) for c in db.recent_adds(conn, SHARE_SOURCE)]


@router.get("/worker")
def worker_state(request: Request) -> WorkerState:
    """추가 화면: 자동 처리 워커가 지금 무엇을 하는지, 멈춘 단계가 있는지."""
    worker = _worker(request)
    return worker.state() if worker is not None else WorkerState(enabled=False)
