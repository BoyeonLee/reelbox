"""보강 단계 전체 흐름: 대상 고르기 → 메타·썸네일 → 자막 또는 받아쓰기 → 상태 기록.

인스타는 요청마다 5~10초 무작위로 기다리고, 최근 24시간 처리 개수가 한도를 넘거나
차단 신호(429, 로그인 요구, 쿠키 만료)가 오면 인스타 처리만 즉시 멈춘다. 차단은 보통
일시적이라 나중에 다시 돌리면 멈춘 곳부터 이어간다.
외부 호출(yt-dlp, whisper, sleep)은 EnrichDeps 로 받아서 테스트에서 가짜로 바꾼다.
"""

import logging
import random
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from reelbox import db
from reelbox.config import Settings
from reelbox.enrich import captions, metadata
from reelbox.enrich.transcribe import NoAudioError, Transcriber, clean_tmp, transcribe_audio
from reelbox.urls import Platform
from reelbox.ytdlp import BlockedError, CookieError

logger = logging.getLogger(__name__)

MAX_ERROR_LEN = 500
THUMB_TRIES = 2  # 썸네일 후보를 몇 개까지 시도할지 (인스타는 시도마다 대기가 붙음)


class Client(Protocol):
    """YtdlpClient 와 같은 모양. 테스트에서는 가짜 클래스."""

    def extract(self, url: str) -> dict[str, Any]: ...
    def fetch_bytes(self, url: str) -> bytes: ...
    def download_audio(self, info: dict[str, Any], dest_dir: Path) -> Path: ...


# 플랫폼 이름 → with 로 여는 클라이언트. 쿠키 설정이 잘못되면 CookieError.
ClientFactory = Callable[[Platform], AbstractContextManager[Client]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


# dataclass: 필드만 적으면 __init__ 을 만들어 주는 표준 라이브러리 기능.
# 함수·객체를 담는 묶음이라 검증이 필요한 Pydantic 모델 대신 쓴다.
@dataclass
class EnrichDeps:
    open_client: ClientFactory
    transcriber: Transcriber
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    now: Callable[[], datetime] = _utcnow


class EnrichReport(BaseModel):
    enriched: int = 0
    failed: int = 0
    unavailable: int = 0
    no_caption: int = 0  # --captions-only 에서 캡션이 없어 건너뛴 항목
    sources: dict[str, int] = {}  # transcripts.source 별 개수 (captions/whisper/none)
    stopped: dict[str, str] = {}  # 멈춘 플랫폼 → 이유
    tmp_cleaned: int = 0


class Throttle:
    """요청 사이에 min~max 초를 무작위로 기다린다. 첫 요청은 바로 보낸다."""

    def __init__(
        self, min_sec: float, max_sec: float, sleep: Callable[[float], None], rng: random.Random
    ) -> None:
        self._min = min_sec
        self._max = max_sec
        self._sleep = sleep
        self._rng = rng
        self._first = True

    def wait(self) -> None:
        if not self._first:
            self._sleep(self._rng.uniform(self._min, self._max))
        self._first = False


class _ThrottledClient:
    """모든 요청 앞에 Throttle.wait() 을 끼워 넣는다 (메타·썸네일·자막·오디오 전부)."""

    def __init__(self, inner: Client, throttle: Throttle) -> None:
        self._inner = inner
        self._throttle = throttle

    def extract(self, url: str) -> dict[str, Any]:
        self._throttle.wait()
        return self._inner.extract(url)

    def fetch_bytes(self, url: str) -> bytes:
        self._throttle.wait()
        return self._inner.fetch_bytes(url)

    def download_audio(self, info: dict[str, Any], dest_dir: Path) -> Path:
        self._throttle.wait()
        return self._inner.download_audio(info, dest_dir)


# --- 오류 분류 ---

# 차단 신호: 그 플랫폼을 바로 멈춘다. 인스타의 'Requested content is not available,
# rate-limit reached or login required' 는 삭제된 글일 수도 있지만 구분할 수 없어서 차단으로
# 본다. 진짜 삭제된 글이면 max_attempts 번 실패한 뒤 대상에서 빠진다.
_BLOCK_SIGNS = (
    "429",
    "too many requests",
    "rate-limit",
    "rate limit",
    "login required",
    "login_required",
    "redirected to the login page",
    "only available for registered users",
    "checkpoint",
    "challenge_required",
    "feedback_required",
    "please wait a few minutes",
    "not a bot",
    "cookies are no longer valid",
)
# 삭제·비공개: unavailable 로 두고 다시 시도하지 않는다.
_UNAVAILABLE_SIGNS = (
    # 인스타 연령·민감 콘텐츠 제한: 내 계정으로는 볼 수 없어서 다시 시도해도 같다
    "isn't available to everyone",
    "can't be seen by certain audiences",
    "video unavailable",
    "this video is not available",
    "this video has been removed",
    "has been terminated",
    "private video",
    "this video is private",
    "no longer available",
    "media not found",
    "post isn't available",
    "http error 404",
    "http error 410",
)

ErrorKind = Literal["blocked", "unavailable", "error"]


def classify(exc: BaseException) -> ErrorKind:
    if isinstance(exc, BlockedError):
        return "blocked"
    text = str(exc).lower()
    if any(sign in text for sign in _BLOCK_SIGNS):
        return "blocked"
    if any(sign in text for sign in _UNAVAILABLE_SIGNS):
        return "unavailable"
    return "error"


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def error_text(exc: BaseException) -> str:
    """DB 에 남길 오류 문장. yt-dlp 의 'ERROR: ' 머리와 색 코드를 뗀다."""
    text = _ANSI.sub("", str(exc)).strip().removeprefix("ERROR: ")
    if not text:
        text = type(exc).__name__
    return text[:MAX_ERROR_LEN]


# --- 항목 하나 ---


def _thumbnail(
    client: Client, meta: metadata.Metadata, item: db.EnrichCandidate, settings: Settings
) -> tuple[str | None, str | None]:
    """(썸네일 URL, 저장 경로). 못 받아도 항목은 실패로 치지 않는다."""
    relpath = metadata.thumbnail_relpath(item.platform, item.source_id)
    for url in meta.thumbnail_urls[:THUMB_TRIES]:
        try:
            metadata.save_thumbnail(client.fetch_bytes(url), settings.data_dir / relpath)
        except Exception as exc:
            logger.warning("썸네일을 받지 못함 (%s): %s", item.url, error_text(exc))
            continue
        return url, relpath
    return (meta.thumbnail_urls[0] if meta.thumbnail_urls else None), None


def _transcript(
    client: Client,
    info: dict[str, Any],
    meta: metadata.Metadata,
    item: db.EnrichCandidate,
    settings: Settings,
    deps: EnrichDeps,
) -> tuple[db.TranscriptSource, str | None, str | None]:
    """(source, language, text)."""
    if meta.media_type == "image":
        return "none", None, None  # 사진 게시물은 캡션만 쓴다

    track = captions.pick_track(info)
    if track is not None:
        try:
            text = captions.fetch_captions(track, client.fetch_bytes)
        except Exception as exc:
            if classify(exc) == "blocked":
                raise
            logger.warning("자막을 받지 못해 받아쓰기로 넘어감 (%s): %s", item.url, error_text(exc))
            text = ""
        if text:
            return "captions", captions.base_lang(track.language), text

    if meta.duration_sec is not None and meta.duration_sec > settings.max_transcribe_sec:
        logger.info("%.0f초 영상이라 받아쓰기를 건너뜀: %s", meta.duration_sec, item.url)
        return "none", None, None

    audio_info = metadata.audio_source(info)
    assert audio_info is not None  # media_type == 'video' 면 있다
    try:
        result = transcribe_audio(
            audio_info, settings.tmp_dir, client.download_audio, deps.transcriber
        )
    except NoAudioError:
        logger.info("소리가 없는 영상: %s", item.url)
        return "none", None, None
    if not result.text:
        return "none", result.language, None  # 말소리가 없음 (음악만 등)
    return "whisper", result.language, result.text


def _enrich_one(
    client: Client, item: db.EnrichCandidate, settings: Settings, deps: EnrichDeps
) -> db.Enrichment:
    info = client.extract(item.url)
    meta = metadata.parse_info(info, item.platform)
    thumb_url, thumb_path = _thumbnail(client, meta, item, settings)
    source, language, text = _transcript(client, info, meta, item, settings, deps)
    return db.Enrichment(
        title=meta.title,
        author=meta.author,
        description=meta.description,
        duration_sec=meta.duration_sec,
        thumbnail_url=thumb_url,
        thumbnail_path=thumb_path,
        media_type=meta.media_type,
        transcript_source=source,
        transcript_language=language,
        transcript_text=text,
    )


# --- 전체 ---


def enrich_items(
    conn: sqlite3.Connection,
    settings: Settings,
    deps: EnrichDeps,
    *,
    platform: Platform | None = None,
    limit: int | None = None,
    on_item: Callable[[int, int, db.EnrichCandidate], None] | None = None,
    source: str | None = None,
) -> EnrichReport:
    """보강할 차례인 항목을 처리한다. 이미 enriched 인 것은 건드리지 않는다(멱등).

    on_item(번호, 전체, 항목) 은 진행 표시용. source 를 주면 그 입구로 들어온 항목만
    (워커가 공유 항목만 처리할 때).
    """
    report = EnrichReport(tmp_cleaned=clean_tmp(settings.tmp_dir))
    candidates = db.enrich_candidates(
        conn, platform=platform, max_attempts=settings.max_attempts, limit=None, source=source
    )
    total = len(candidates) if limit is None else min(limit, len(candidates))
    processed = 0

    # ExitStack: 여러 개의 with 를 한꺼번에 관리한다. 플랫폼별 클라이언트를 처음 필요할 때
    # 열고, 함수를 나갈 때(예외가 나도) 전부 닫는다.
    with ExitStack() as stack:
        clients: dict[str, Client] = {}
        for item in candidates:
            if limit is not None and processed >= limit:
                break
            if item.platform in report.stopped:
                continue
            if item.platform == "instagram":
                since = deps.now() - timedelta(hours=24)
                used = db.fetch_count_since(conn, "instagram", since)
                if used >= settings.instagram_daily_limit:
                    report.stopped["instagram"] = (
                        f"일일 한도 도달 (최근 24시간 {used}개 / 한도 "
                        f"{settings.instagram_daily_limit}개). 나중에 다시 실행하면 이어서 한다."
                    )
                    continue

            client = clients.get(item.platform)
            if client is None:
                try:
                    client = stack.enter_context(deps.open_client(item.platform))
                except CookieError as exc:
                    report.stopped[item.platform] = " ".join(
                        filter(None, [exc.cause, exc.fix, exc.detail])
                    )
                    continue
                if item.platform == "instagram":
                    throttle = Throttle(
                        settings.instagram_delay_min_sec,
                        settings.instagram_delay_max_sec,
                        deps.sleep,
                        deps.rng,
                    )
                    client = _ThrottledClient(client, throttle)
                clients[item.platform] = client

            processed += 1
            if on_item is not None:
                on_item(processed, total, item)
            db.log_fetch(conn, item.platform, item.id, deps.now())
            try:
                enrichment = _enrich_one(client, item, settings, deps)
            except Exception as exc:
                kind = classify(exc)
                message = error_text(exc)
                if kind == "unavailable":
                    db.mark_unavailable(conn, item.id, message)
                    report.unavailable += 1
                else:
                    db.mark_failed(conn, item.id, message)
                    report.failed += 1
                    if kind == "blocked":
                        report.stopped[item.platform] = f"차단 신호: {message}"
                logger.warning("보강 실패 (%s, %s): %s", kind, item.url, message)
                continue

            db.save_enrichment(conn, item.id, enrichment)
            report.enriched += 1
            source = enrichment.transcript_source
            report.sources[source] = report.sources.get(source, 0) + 1
    return report


def enrich_from_captions(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    platform: Platform | None = None,
    limit: int | None = None,
) -> EnrichReport:
    """네트워크 없이 이미 있는 캡션(인스타 내보내기 파일)만으로 enriched 로 넘긴다.

    인스타가 막혀서 보강이 안 될 때 쓴다. 자막·썸네일은 없고 transcripts 는 'none'.
    캡션이 없는 항목은 그대로 둔다.
    """
    report = EnrichReport()
    candidates = db.enrich_candidates(
        conn, platform=platform, max_attempts=settings.max_attempts, limit=None
    )
    for item in candidates:
        if limit is not None and report.enriched >= limit:
            break
        if not item.description:
            report.no_caption += 1
            continue
        db.save_enrichment(conn, item.id, db.Enrichment(transcript_source="none"))
        report.enriched += 1
    report.sources = {"none": report.enriched} if report.enriched else {}
    return report
