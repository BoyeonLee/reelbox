"""SQLite 연결, 번호별 마이그레이션, 저장소 함수."""

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

import sqlite_vec
from pydantic import BaseModel

from reelbox.config import Settings
from reelbox.urls import MediaType, Platform, parse_url

logger = logging.getLogger(__name__)

# DESIGN.md 4장 "status 흐름" 순서
ITEM_STATUSES: tuple[str, ...] = (
    "imported",
    "enriched",
    "analyzed",
    "indexed",
    "failed",
    "unavailable",
    "skipped",
    "deleted",
)

_MIGRATION_RE = re.compile(r"^(\d{3})_[\w-]+\.sql$")


class InitResult(BaseModel):
    db_path: Path
    applied: list[str]
    version: int


class FailedItem(BaseModel):
    id: int
    platform: str
    url: str
    error: str | None
    attempts: int
    updated_at: str


def connect(db_path: Path) -> sqlite3.Connection:
    """sqlite-vec 을 불러온 연결을 연다. 확장을 못 불러오면 예외를 그대로 올린다."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _migration_files(migrations_dir: Path) -> list[tuple[int, Path]]:
    files: list[tuple[int, Path]] = []
    for path in migrations_dir.glob("*.sql"):
        m = _MIGRATION_RE.match(path.name)
        if m is None:
            raise ValueError(f"마이그레이션 파일 이름 형식이 틀림 (NNN_name.sql): {path.name}")
        files.append((int(m.group(1)), path))
    files.sort()
    numbers = [n for n, _ in files]
    if len(numbers) != len(set(numbers)):
        raise ValueError(f"마이그레이션 번호가 중복됨: {numbers}")
    return files


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, migrations_dir: Path) -> list[str]:
    """아직 적용 안 된 마이그레이션을 번호순으로 적용하고, 적용한 파일 이름을 돌려준다.

    적용한 마지막 번호는 PRAGMA user_version 에 기록한다. 파일 하나는 트랜잭션 하나라서
    중간에 실패하면 그 파일 전체가 되돌려진다.
    """
    current = schema_version(conn)
    applied: list[str] = []
    for number, path in _migration_files(migrations_dir):
        if number <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        logger.info("마이그레이션 적용: %s", path.name)
        applied.append(path.name)
    return applied


def init_db(settings: Settings) -> InitResult:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        applied = migrate(conn, settings.migrations_dir)
        return InitResult(db_path=settings.db_path, applied=applied, version=schema_version(conn))
    finally:
        conn.close()


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """상태별 항목 수. 항목이 없는 상태도 0으로 포함한다."""
    counts = dict.fromkeys(ITEM_STATUSES, 0)
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status"):
        counts[row["status"]] = row["n"]
    return counts


def recent_failures(conn: sqlite3.Connection, limit: int = 5) -> list[FailedItem]:
    rows = conn.execute(
        """
        SELECT id, platform, url, error, attempts, updated_at
        FROM items
        WHERE status = 'failed'
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [FailedItem(**dict(row)) for row in rows]


class AddOutcome(StrEnum):
    ADDED = "added"
    DUPLICATE = "duplicate"
    DELETED = "deleted"  # 휴지통에서 영구 삭제한 영상 → 다시 넣지 않음
    UNRECOGNIZED = "unrecognized"


def to_iso(dt: datetime) -> str:
    """DB 에 넣는 시각 형식 (ISO-8601 UTC, 초 단위)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_item(
    conn: sqlite3.Connection,
    url: str,
    *,
    saved_at: datetime | None,
    source: str,
    collection: str | None = None,
    description: str | None = None,
    author: str | None = None,
) -> AddOutcome:
    """모든 입구(내보내기·동기화·URL 목록·공유)가 쓰는 항목 추가 함수. 커밋은 부르는 쪽이 한다.

    이미 있는 영상이면 새로 만들지 않고, 비어 있던 saved_at·collection·description·author 만
    채운다. 그래서 같은 파일을 여러 번 넣어도 결과가 같다(멱등).
    description·author 는 인스타 내보내기 파일의 캡션·작성자 (보강 전에도 분석할 수 있게).
    """
    parsed = parse_url(url)
    if parsed is None:
        return AddOutcome.UNRECOGNIZED
    values = {
        "platform": parsed.platform,
        "source_id": parsed.source_id,
        "url": parsed.url,
        "saved_at": to_iso(saved_at) if saved_at else None,
        "source": source,
        "collection": collection,
        "media_type": parsed.media_type,
        "description": description or None,
        "author": author or None,
    }

    cur = conn.execute(
        """
        INSERT INTO items (platform, source_id, url, saved_at, imported_from, collection,
                           media_type, description, author)
        VALUES (:platform, :source_id, :url, :saved_at, :source, :collection,
                :media_type, :description, :author)
        ON CONFLICT (platform, source_id) DO NOTHING
        """,
        values,
    )
    if cur.rowcount == 1:
        return AddOutcome.ADDED

    row = conn.execute(
        "SELECT id, status FROM items WHERE platform = ? AND source_id = ?",
        (parsed.platform, parsed.source_id),
    ).fetchone()
    if row["status"] == "deleted":
        return AddOutcome.DELETED
    # 채울 게 있을 때만 UPDATE 한다 (안 그러면 트리거가 updated_at 을 매번 바꾼다)
    conn.execute(
        """
        UPDATE items
        SET saved_at = COALESCE(saved_at, :saved_at),
            collection = COALESCE(collection, :collection),
            description = COALESCE(description, :description),
            author = COALESCE(author, :author)
        WHERE id = :id AND (
            (saved_at IS NULL AND :saved_at IS NOT NULL)
            OR (collection IS NULL AND :collection IS NOT NULL)
            OR (description IS NULL AND :description IS NOT NULL)
            OR (author IS NULL AND :author IS NOT NULL))
        """,
        {**values, "id": row["id"]},
    )
    return AddOutcome.DUPLICATE


# --- 보강(enrich) 단계 ---


class EnrichCandidate(BaseModel):
    id: int
    platform: Platform
    source_id: str
    url: str
    media_type: MediaType | None
    description: str | None
    status: str
    attempts: int


def enrich_candidates(
    conn: sqlite3.Connection,
    *,
    platform: str | None,
    max_attempts: int,
    limit: int | None,
) -> list[EnrichCandidate]:
    """보강할 차례인 항목. imported 먼저, 그다음 다시 시도할 failed.

    failed 는 다른 단계(analyze 등)에서도 쓰므로, 보강 결과(transcripts)가 없는 것만
    보강 단계의 실패로 본다.
    """
    rows = conn.execute(
        """
        SELECT id, platform, source_id, url, media_type, description, status, attempts
        FROM items AS i
        WHERE (status = 'imported'
               OR (status = 'failed' AND attempts < :max_attempts
                   AND NOT EXISTS (SELECT 1 FROM transcripts AS t WHERE t.item_id = i.id)))
          AND (:platform IS NULL OR platform = :platform)
        ORDER BY status = 'failed', id
        LIMIT :limit
        """,
        {
            "max_attempts": max_attempts,
            "platform": platform,
            "limit": -1 if limit is None else limit,
        },
    )
    return [EnrichCandidate(**dict(r)) for r in rows]


def log_fetch(conn: sqlite3.Connection, platform: str, item_id: int | None, at: datetime) -> None:
    """플랫폼에 요청한 기록 한 줄. 일일 한도를 세는 데 쓴다. 바로 커밋한다."""
    with conn:
        conn.execute(
            "INSERT INTO fetch_log (platform, item_id, fetched_at) VALUES (?, ?, ?)",
            (platform, item_id, to_iso(at)),
        )


def fetch_count_since(conn: sqlite3.Connection, platform: str, since: datetime) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE platform = ? AND fetched_at >= ?",
        (platform, to_iso(since)),
    ).fetchone()[0]


TranscriptSource = Literal["captions", "whisper", "none"]


class Enrichment(BaseModel):
    """보강 결과. None 인 메타 값은 기존 값을 지우지 않는다 (내보내기 캡션 등을 남김)."""

    title: str | None = None
    author: str | None = None
    description: str | None = None
    duration_sec: float | None = None
    thumbnail_url: str | None = None
    thumbnail_path: str | None = None  # data_dir 기준 상대 경로
    media_type: MediaType | None = None
    transcript_source: TranscriptSource = "none"
    transcript_language: str | None = None
    transcript_text: str | None = None


def save_enrichment(conn: sqlite3.Connection, item_id: int, e: Enrichment) -> None:
    """items 를 채우고 transcripts 를 넣은 뒤 enriched 로 바꾼다. 한 트랜잭션."""
    with conn:
        conn.execute(
            """
            UPDATE items
            SET title = COALESCE(:title, title),
                author = COALESCE(:author, author),
                description = COALESCE(:description, description),
                duration_sec = COALESCE(:duration_sec, duration_sec),
                thumbnail_url = COALESCE(:thumbnail_url, thumbnail_url),
                thumbnail_path = COALESCE(:thumbnail_path, thumbnail_path),
                media_type = COALESCE(:media_type, media_type),
                status = 'enriched',
                error = NULL
            WHERE id = :id
            """,
            {**e.model_dump(), "id": item_id},
        )
        conn.execute(
            """
            INSERT INTO transcripts (item_id, source, language, text) VALUES (?, ?, ?, ?)
            ON CONFLICT (item_id) DO UPDATE
            SET source = excluded.source, language = excluded.language, text = excluded.text
            """,
            (item_id, e.transcript_source, e.transcript_language, e.transcript_text),
        )


def mark_failed(conn: sqlite3.Connection, item_id: int, error: str) -> None:
    with conn:
        conn.execute(
            "UPDATE items SET status = 'failed', error = ?, attempts = attempts + 1 WHERE id = ?",
            (error, item_id),
        )


def mark_unavailable(conn: sqlite3.Connection, item_id: int, error: str) -> None:
    """삭제·비공개 영상. 목록에는 남기고 다시 시도하지 않는다."""
    with conn:
        conn.execute(
            "UPDATE items SET status = 'unavailable', error = ? WHERE id = ?", (error, item_id)
        )


# --- AI 분석(analyze) 단계 ---


class AnalyzeInput(BaseModel):
    id: int
    platform: Platform
    url: str
    media_type: MediaType | None
    title: str | None
    author: str | None
    description: str | None
    collection: str | None
    transcript_source: TranscriptSource | None
    transcript_text: str | None


def analyze_candidates(
    conn: sqlite3.Connection, *, max_attempts: int, limit: int | None
) -> list[AnalyzeInput]:
    """분석할 차례인 항목. enriched 먼저, 그다음 분석 단계에서 실패한 것.

    보강 결과(transcripts)는 있고 분석 결과(analyses)는 없는 failed 를 분석 단계 실패로 본다.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.platform, i.url, i.media_type, i.title, i.author, i.description,
               i.collection, t.source AS transcript_source, t.text AS transcript_text
        FROM items AS i
        JOIN transcripts AS t ON t.item_id = i.id
        WHERE (i.status = 'enriched'
               OR (i.status = 'failed' AND i.attempts < :max_attempts
                   AND NOT EXISTS (SELECT 1 FROM analyses AS a WHERE a.item_id = i.id)))
        ORDER BY i.status = 'failed', i.id
        LIMIT :limit
        """,
        {"max_attempts": max_attempts, "limit": -1 if limit is None else limit},
    )
    return [AnalyzeInput(**dict(r)) for r in rows]


class AnalysisRecord(BaseModel):
    summary_ko: str
    key_points: list[str]
    raw_topic: str
    tags: list[str]
    content_language: str
    model: str
    prompt_version: str


def save_analysis(conn: sqlite3.Connection, item_id: int, a: AnalysisRecord) -> None:
    """analyses 를 넣고(있으면 바꾸고) analyzed 로 바꾼다. 한 트랜잭션."""
    with conn:
        conn.execute(
            """
            INSERT INTO analyses (item_id, summary_ko, key_points, tags, raw_topic,
                                  content_language, model, prompt_version)
            VALUES (:id, :summary_ko, :key_points, :tags, :raw_topic,
                    :content_language, :model, :prompt_version)
            ON CONFLICT (item_id) DO UPDATE SET
                summary_ko = excluded.summary_ko, key_points = excluded.key_points,
                tags = excluded.tags, raw_topic = excluded.raw_topic,
                content_language = excluded.content_language, model = excluded.model,
                prompt_version = excluded.prompt_version,
                created_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            {
                **a.model_dump(),
                "key_points": json.dumps(a.key_points, ensure_ascii=False),
                "tags": json.dumps(a.tags, ensure_ascii=False),
                "id": item_id,
            },
        )
        conn.execute("UPDATE items SET status = 'analyzed', error = NULL WHERE id = ?", (item_id,))


class AnalysisView(BaseModel):
    """review 명령에서 보여줄 한 줄."""

    id: int
    platform: str
    title: str | None
    author: str | None
    summary_ko: str | None
    raw_topic: str | None
    tags: list[str]
    created_at: str


def recent_analyses(conn: sqlite3.Connection, limit: int) -> list[AnalysisView]:
    rows = conn.execute(
        """
        SELECT i.id, i.platform, i.title, i.author, a.summary_ko, a.raw_topic, a.tags,
               a.created_at
        FROM analyses AS a JOIN items AS i ON i.id = a.item_id
        ORDER BY a.created_at DESC, a.item_id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [AnalysisView(**{**dict(r), "tags": json.loads(r["tags"])}) for r in rows]
