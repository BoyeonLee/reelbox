"""SQLite 연결, 번호별 마이그레이션, 저장소 함수."""

import json
import logging
import re
import sqlite3
import struct
from collections.abc import Sequence
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


def connect(db_path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """sqlite-vec 을 불러온 연결을 연다. 확장을 못 불러오면 예외를 그대로 올린다.

    check_same_thread=False 는 웹 서버용: 한 요청이 연결을 순서대로만 쓰지만, FastAPI 가
    열기와 닫기를 서로 다른 스레드에서 할 수 있다.
    """
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
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
        """
        SELECT i.id, i.status, um.deleted_at
        FROM items AS i LEFT JOIN user_meta AS um ON um.item_id = i.id
        WHERE i.platform = ? AND i.source_id = ?
        """,
        (parsed.platform, parsed.source_id),
    ).fetchone()
    # 휴지통에 있는 것(deleted_at)도 영구 삭제한 것과 같이 건너뛴다.
    # 공유로 다시 들어온 경우의 복원은 웹 API 가 따로 처리한다 (DESIGN.md 4장).
    if row["status"] == "deleted" or row["deleted_at"] is not None:
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


# --- 공통: 목록·검색·주제에 보여줄 항목 ---

# 숨김·삭제(휴지통) 항목과 영구 삭제된 항목을 뺀다 (DESIGN.md 4장 "숨기기와 삭제").
# items 별칭이 i 인 쿼리의 WHERE 에 붙여 쓴다.
VISIBLE_SQL = """i.status != 'deleted' AND NOT EXISTS (
    SELECT 1 FROM user_meta AS um
    WHERE um.item_id = i.id AND (um.hidden_at IS NOT NULL OR um.deleted_at IS NOT NULL))"""


def vector_to_blob(vector: Sequence[float]) -> bytes:
    """sqlite-vec 이 받는 형식: float32 를 이어 붙인 바이트."""
    return struct.pack(f"{len(vector)}f", *vector)


def blob_to_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


# --- 임베딩 (organize 와 index 가 같이 쓴다) ---


class EmbedInput(BaseModel):
    """임베딩용 텍스트를 만들 재료. 색인 단계는 FTS 에 넣을 설명도 같이 쓴다."""

    id: int
    title: str | None
    description: str | None
    transcript_text: str | None
    summary_ko: str | None
    raw_topic: str | None
    tags: list[str]


_EMBED_INPUT_SQL = """
    SELECT i.id, i.title, i.description, t.text AS transcript_text,
           a.summary_ko, a.raw_topic, a.tags
    FROM items AS i
    JOIN analyses AS a ON a.item_id = i.id
    LEFT JOIN transcripts AS t ON t.item_id = i.id
"""


def _embed_input(row: sqlite3.Row) -> EmbedInput:
    return EmbedInput(**{**dict(row), "tags": json.loads(row["tags"])})


def load_vectors(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, list[float]]:
    """저장된 임베딩. 없는 항목은 결과에 없다."""
    if not item_ids:
        return {}
    marks = ", ".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT item_id, vector FROM embeddings WHERE item_id IN ({marks})", item_ids
    )
    return {r["item_id"]: blob_to_vector(r["vector"]) for r in rows}


def save_vectors(conn: sqlite3.Connection, vectors: dict[int, list[float]]) -> None:
    """임베딩을 넣는다(있으면 바꾼다). vec0 테이블은 ON CONFLICT 를 몰라서 지우고 넣는다."""
    with conn:
        for item_id, vector in vectors.items():
            conn.execute("DELETE FROM embeddings WHERE item_id = ?", (item_id,))
            conn.execute(
                "INSERT INTO embeddings (item_id, vector) VALUES (?, ?)",
                (item_id, vector_to_blob(vector)),
            )


# --- 색인(index) 단계 ---


def index_candidates(conn: sqlite3.Connection, *, limit: int | None) -> list[EmbedInput]:
    """색인할 차례인 항목 (analyzed). 숨김·삭제 항목도 색인은 해 둔다 (검색할 때 뺀다)."""
    rows = conn.execute(
        _EMBED_INPUT_SQL + "WHERE i.status = 'analyzed' ORDER BY i.id LIMIT ?",
        (-1 if limit is None else limit,),
    )
    return [_embed_input(r) for r in rows]


def save_index(conn: sqlite3.Connection, item: EmbedInput) -> None:
    """FTS 에 넣고(있으면 바꾸고) indexed 로 바꾼다. 임베딩은 먼저 save_vectors 로 넣는다."""
    with conn:
        conn.execute("DELETE FROM items_fts WHERE rowid = ?", (item.id,))
        conn.execute(
            """
            INSERT INTO items_fts (rowid, title, description, transcript, summary, tags)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.title or "",
                item.description or "",
                item.transcript_text or "",
                item.summary_ko or "",
                " ".join(item.tags),
            ),
        )
        conn.execute("UPDATE items SET status = 'indexed', error = NULL WHERE id = ?", (item.id,))


def indexed_vectors(conn: sqlite3.Connection) -> dict[int, list[float]]:
    """비슷한 영상 계산에 쓸, 보여줄 수 있는 색인 항목 전부의 임베딩."""
    rows = conn.execute(
        f"""
        SELECT e.item_id, e.vector
        FROM embeddings AS e JOIN items AS i ON i.id = e.item_id
        WHERE i.status = 'indexed' AND {VISIBLE_SQL}
        ORDER BY e.item_id
        """
    )
    return {r["item_id"]: blob_to_vector(r["vector"]) for r in rows}


def replace_similar(conn: sqlite3.Connection, rows: list[tuple[int, int, int, float]]) -> None:
    """item_similar 를 통째로 바꾼다. rows = (item_id, similar_id, rank, score)."""
    with conn:
        conn.execute("DELETE FROM item_similar")
        conn.executemany(
            "INSERT INTO item_similar (item_id, similar_id, rank, score) VALUES (?, ?, ?, ?)",
            rows,
        )


def similar_ids(conn: sqlite3.Connection, item_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT similar_id FROM item_similar WHERE item_id = ? ORDER BY rank", (item_id,)
    )
    return [r["similar_id"] for r in rows]


# --- 주제 정리(organize) 단계 ---


class Topic(BaseModel):
    id: int
    name: str
    description: str | None
    is_user_edited: bool


class TopicCount(Topic):
    items: int  # 보여줄 수 있는 항목 수


def list_topics(conn: sqlite3.Connection) -> list[Topic]:
    rows = conn.execute("SELECT id, name, description, is_user_edited FROM topics ORDER BY id")
    return [Topic(**dict(r)) for r in rows]


def topic_counts(conn: sqlite3.Connection) -> list[TopicCount]:
    """주제별 항목 수 (대표 주제 기준, 숨김·삭제 제외). 많은 순."""
    rows = conn.execute(
        f"""
        SELECT t.id, t.name, t.description, t.is_user_edited,
               (SELECT COUNT(*) FROM item_topics AS it JOIN items AS i ON i.id = it.item_id
                WHERE it.topic_id = t.id AND it.is_primary = 1 AND {VISIBLE_SQL}) AS items
        FROM topics AS t
        ORDER BY items DESC, t.id
        """
    )
    return [TopicCount(**dict(r)) for r in rows]


def raw_topic_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """분석된(보여줄 수 있는) 항목의 raw_topic 과 개수. 많은 순."""
    rows = conn.execute(
        f"""
        SELECT a.raw_topic, COUNT(*) AS n
        FROM analyses AS a JOIN items AS i ON i.id = a.item_id
        WHERE i.status IN ('analyzed', 'indexed') AND a.raw_topic IS NOT NULL AND {VISIBLE_SQL}
        GROUP BY a.raw_topic
        ORDER BY n DESC, a.raw_topic
        """
    )
    return [(r["raw_topic"], r["n"]) for r in rows]


def unassigned_items(conn: sqlite3.Connection, *, limit: int | None) -> list[EmbedInput]:
    """분석은 끝났는데 주제가 없는 (보여줄 수 있는) 항목."""
    rows = conn.execute(
        _EMBED_INPUT_SQL
        + f"""
        WHERE i.status IN ('analyzed', 'indexed') AND {VISIBLE_SQL}
          AND NOT EXISTS (SELECT 1 FROM item_topics AS it WHERE it.item_id = i.id)
        ORDER BY i.id
        LIMIT ?
        """,
        (-1 if limit is None else limit,),
    )
    return [_embed_input(r) for r in rows]


def raw_topic_assignments(conn: sqlite3.Connection) -> dict[str, int]:
    """raw_topic → 그 raw_topic 항목들이 가장 많이 들어간 대표 주제. 새 항목 배정에 재사용한다."""
    rows = conn.execute(
        """
        SELECT a.raw_topic, it.topic_id, COUNT(*) AS n
        FROM analyses AS a JOIN item_topics AS it ON it.item_id = a.item_id AND it.is_primary = 1
        WHERE a.raw_topic IS NOT NULL
        GROUP BY a.raw_topic, it.topic_id
        ORDER BY n ASC, it.topic_id DESC
        """
    )
    # 개수 오름차순으로 덮어쓰므로 마지막(가장 많은) 주제가 남는다
    return {r["raw_topic"]: r["topic_id"] for r in rows}


def topic_raw_topics(conn: sqlite3.Connection, per_topic: int) -> dict[int, list[str]]:
    """주제마다 들어 있는 raw_topic 몇 개 (많은 순). 주제 임베딩 텍스트에 쓴다."""
    result: dict[int, list[str]] = {}
    rows = conn.execute(
        """
        SELECT it.topic_id, a.raw_topic, COUNT(*) AS n
        FROM item_topics AS it JOIN analyses AS a ON a.item_id = it.item_id
        WHERE it.is_primary = 1 AND a.raw_topic IS NOT NULL
        GROUP BY it.topic_id, a.raw_topic
        ORDER BY it.topic_id, n DESC, a.raw_topic
        """
    )
    for r in rows:
        names = result.setdefault(r["topic_id"], [])
        if len(names) < per_topic:
            names.append(r["raw_topic"])
    return result


class NewTopic(BaseModel):
    name: str
    description: str


def replace_topics(
    conn: sqlite3.Connection,
    new_topics: list[NewTopic],
    raw_to_topic: dict[str, int | str],
) -> dict[str, int]:
    """전체 재정리 결과를 한 트랜잭션으로 쓴다.

    - 자동 주제(is_user_edited = 0)는 지우고 new_topics 를 새로 만든다.
    - 내가 고친 주제는 그대로 둔다.
    - 모든 주제 연결을 지운 뒤 raw_to_topic 대로 다시 잇는다. 값이 int 면 기존(고정) 주제 id,
      str 이면 new_topics 의 이름이다.
    돌려주는 값: 주제 이름 → id (새로 만든 것과 고정 주제 모두).
    """
    with conn:
        conn.execute("DELETE FROM item_topics")
        conn.execute("DELETE FROM topics WHERE is_user_edited = 0")
        name_to_id = {t.name: t.id for t in list_topics(conn)}
        for t in new_topics:
            cur = conn.execute(
                "INSERT INTO topics (name, description) VALUES (?, ?)", (t.name, t.description)
            )
            name_to_id[t.name] = cur.lastrowid  # type: ignore[assignment]
        resolved = {
            raw: (target if isinstance(target, int) else name_to_id[target])
            for raw, target in raw_to_topic.items()
        }
        rows = conn.execute(
            f"""
            SELECT i.id, a.raw_topic
            FROM items AS i JOIN analyses AS a ON a.item_id = i.id
            WHERE i.status IN ('analyzed', 'indexed') AND {VISIBLE_SQL}
            """
        ).fetchall()
        conn.executemany(
            "INSERT INTO item_topics (item_id, topic_id, is_primary) VALUES (?, ?, 1)",
            [(r["id"], resolved[r["raw_topic"]]) for r in rows if r["raw_topic"] in resolved],
        )
    return name_to_id


def assign_topics(conn: sqlite3.Connection, assignments: dict[int, int]) -> None:
    """항목 id → 대표 주제 id. 이미 대표 주제가 있으면 바꾼다."""
    with conn:
        for item_id, topic_id in assignments.items():
            conn.execute("DELETE FROM item_topics WHERE item_id = ? AND is_primary = 1", (item_id,))
            conn.execute(
                "INSERT OR REPLACE INTO item_topics (item_id, topic_id, is_primary)"
                " VALUES (?, ?, 1)",
                (item_id, topic_id),
            )


# --- 웹 화면: 목록·상세 ---

# 목록에 보여줄 상태. 분석 전 항목은 제목·요약이 없어서 목록에는 넣지 않는다.
LISTABLE_STATUSES = ("analyzed", "indexed")

# 저장 시각이 없으면(유튜브 목록 등) 처음 가져온 시각으로 정렬한다
_SORT_KEY = "COALESCE(i.saved_at, i.created_at)"

_CARD_COLUMNS = """
    i.id, i.platform, i.url, i.title, i.author, i.saved_at, i.created_at,
    i.duration_sec, i.thumbnail_path, i.media_type, i.status, i.error,
    a.summary_ko, tp.id AS topic_id, tp.name AS topic, um.hidden_at, um.deleted_at
"""
_CARD_FROM = """
    FROM items AS i
    LEFT JOIN analyses AS a ON a.item_id = i.id
    LEFT JOIN item_topics AS it ON it.item_id = i.id AND it.is_primary = 1
    LEFT JOIN topics AS tp ON tp.id = it.topic_id
    LEFT JOIN user_meta AS um ON um.item_id = i.id
"""
_CARD_SQL = f"SELECT {_CARD_COLUMNS} {_CARD_FROM}"


class ItemCard(BaseModel):
    """목록 카드 한 장에 필요한 값."""

    id: int
    platform: Platform
    url: str
    title: str | None
    author: str | None
    summary_ko: str | None
    topic_id: int | None
    topic: str | None
    saved_at: str | None
    created_at: str
    duration_sec: float | None
    thumbnail_path: str | None
    media_type: MediaType | None
    status: str
    error: str | None
    hidden_at: str | None
    deleted_at: str | None


class ItemDetail(ItemCard):
    description: str | None
    key_points: list[str]
    tags: list[str]
    transcript_source: str | None
    transcript_text: str | None
    similar: list[ItemCard]


def _listable_where() -> str:
    statuses = ", ".join(f"'{s}'" for s in LISTABLE_STATUSES)
    return f"i.status IN ({statuses}) AND {VISIBLE_SQL}"


def list_items(
    conn: sqlite3.Connection,
    *,
    topic_id: int | None = None,
    oldest_first: bool = False,
    limit: int = 30,
    offset: int = 0,
) -> list[ItemCard]:
    """홈 최근 저장·주제 상세 목록. 숨김·삭제 항목은 뺀다."""
    where = _listable_where()
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if topic_id is not None:
        where += " AND tp.id = :topic_id"
        params["topic_id"] = topic_id
    order = "ASC" if oldest_first else "DESC"
    rows = conn.execute(
        _CARD_SQL
        + f"WHERE {where} ORDER BY {_SORT_KEY} {order}, i.id {order} LIMIT :limit OFFSET :offset",
        params,
    )
    return [ItemCard(**dict(r)) for r in rows]


def count_listable(conn: sqlite3.Connection) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM items AS i WHERE {_listable_where()}").fetchone()[0]


def cards_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[ItemCard]:
    """id 순서를 지켜서 카드를 돌려준다 (검색 결과·비슷한 영상). 없는 id 는 빠진다."""
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(_CARD_SQL + f"WHERE i.id IN ({marks})", ids)
    by_id = {r["id"]: ItemCard(**dict(r)) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def get_item_detail(conn: sqlite3.Connection, item_id: int) -> ItemDetail | None:
    """상세 화면. 숨김·휴지통 항목도 보여준다 (되돌리기용). 영구 삭제된 항목은 None."""
    row = conn.execute(
        f"""
        SELECT {_CARD_COLUMNS}, i.description, a.key_points, a.tags,
               tr.source AS transcript_source, tr.text AS transcript_text
        {_CARD_FROM}
        LEFT JOIN transcripts AS tr ON tr.item_id = i.id
        WHERE i.id = ? AND i.status != 'deleted'
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    similar = [
        c
        for c in cards_by_ids(conn, similar_ids(conn, item_id))
        if c.hidden_at is None and c.deleted_at is None and c.status in LISTABLE_STATUSES
    ]
    return ItemDetail(
        **{
            **data,
            "key_points": json.loads(data["key_points"] or "[]"),
            "tags": json.loads(data["tags"] or "[]"),
            "similar": similar,
        }
    )


class TopicCard(BaseModel):
    id: int
    name: str
    description: str | None
    items: int
    thumbnail_paths: list[str]


def topic_cards(conn: sqlite3.Connection, thumbs: int = 3) -> list[TopicCard]:
    """홈의 주제 카드: 개수와 최근 저장 순 대표 썸네일. 보여줄 항목이 0개인 주제는 뺀다."""
    cards: list[TopicCard] = []
    for t in topic_counts(conn):
        if t.items == 0:
            continue
        rows = conn.execute(
            f"""
            SELECT i.thumbnail_path
            FROM item_topics AS it JOIN items AS i ON i.id = it.item_id
            WHERE it.topic_id = ? AND it.is_primary = 1 AND i.thumbnail_path IS NOT NULL
              AND {VISIBLE_SQL}
            ORDER BY {_SORT_KEY} DESC, i.id DESC
            LIMIT ?
            """,
            (t.id, thumbs),
        )
        cards.append(
            TopicCard(
                id=t.id,
                name=t.name,
                description=t.description,
                items=t.items,
                thumbnail_paths=[r["thumbnail_path"] for r in rows],
            )
        )
    return cards


def get_topic(conn: sqlite3.Connection, topic_id: int) -> TopicCount | None:
    for t in topic_counts(conn):
        if t.id == topic_id:
            return t
    return None


def list_hidden(conn: sqlite3.Connection) -> list[ItemCard]:
    """숨긴 항목 (휴지통에 있는 것은 뺀다). 최근에 숨긴 순."""
    rows = conn.execute(
        _CARD_SQL + "WHERE um.hidden_at IS NOT NULL AND um.deleted_at IS NULL"
        " AND i.status != 'deleted' ORDER BY um.hidden_at DESC, i.id DESC"
    )
    return [ItemCard(**dict(r)) for r in rows]


def list_trash(conn: sqlite3.Connection) -> list[ItemCard]:
    """휴지통 항목. 최근에 버린 순."""
    rows = conn.execute(
        _CARD_SQL + "WHERE um.deleted_at IS NOT NULL AND i.status != 'deleted'"
        " ORDER BY um.deleted_at DESC, i.id DESC"
    )
    return [ItemCard(**dict(r)) for r in rows]


def recent_adds(conn: sqlite3.Connection, source: str, limit: int = 20) -> list[ItemCard]:
    """추가 화면에서 처리 상태를 보여줄, 최근에 source 로 들어온 항목."""
    rows = conn.execute(
        _CARD_SQL + "WHERE i.imported_from = ? AND i.status != 'deleted'"
        " ORDER BY i.created_at DESC, i.id DESC LIMIT ?",
        (source, limit),
    )
    return [ItemCard(**dict(r)) for r in rows]


def find_item_id(conn: sqlite3.Connection, platform: str, source_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM items WHERE platform = ? AND source_id = ?", (platform, source_id)
    ).fetchone()
    return None if row is None else row["id"]


def is_trashed(conn: sqlite3.Connection, item_id: int) -> bool:
    """휴지통에 있는지 (영구 삭제 전)."""
    row = conn.execute(
        """
        SELECT 1 FROM items AS i JOIN user_meta AS um ON um.item_id = i.id
        WHERE i.id = ? AND um.deleted_at IS NOT NULL AND i.status != 'deleted'
        """,
        (item_id,),
    ).fetchone()
    return row is not None


# --- 숨기기·삭제(휴지통)·복원 (DESIGN.md 4장 "숨기기와 삭제") ---


def _existing_ids(conn: sqlite3.Connection, ids: Sequence[int]) -> list[int]:
    """영구 삭제되지 않은, 실제로 있는 항목 id 만 (순서·중복 정리)."""
    unique = list(dict.fromkeys(ids))
    if not unique:
        return []
    marks = ", ".join("?" for _ in unique)
    rows = conn.execute(
        f"SELECT id FROM items WHERE id IN ({marks}) AND status != 'deleted'", unique
    )
    found = {r["id"] for r in rows}
    return [i for i in unique if i in found]


def _set_meta_time(
    conn: sqlite3.Connection, ids: Sequence[int], column: str, value: str | None
) -> list[int]:
    """user_meta 의 hidden_at 또는 deleted_at 을 바꾼다. 바꾼 항목 id 를 돌려준다."""
    assert column in ("hidden_at", "deleted_at")
    targets = _existing_ids(conn, ids)
    with conn:
        for item_id in targets:
            conn.execute(
                f"""
                INSERT INTO user_meta (item_id, {column}) VALUES (?, ?)
                ON CONFLICT (item_id) DO UPDATE SET {column} = excluded.{column}
                """,
                (item_id, value),
            )
    return targets


def set_hidden(
    conn: sqlite3.Connection, ids: Sequence[int], hidden: bool, *, now: datetime
) -> list[int]:
    return _set_meta_time(conn, ids, "hidden_at", to_iso(now) if hidden else None)


def trash_items(conn: sqlite3.Connection, ids: Sequence[int], *, now: datetime) -> list[int]:
    """휴지통으로. 30일 안에는 restore_items 로 되돌릴 수 있다."""
    return _set_meta_time(conn, ids, "deleted_at", to_iso(now))


def restore_items(conn: sqlite3.Connection, ids: Sequence[int]) -> list[int]:
    """휴지통에서 꺼낸다. 숨김 표시는 그대로 둔다."""
    return _set_meta_time(conn, ids, "deleted_at", None)


class PurgedItem(BaseModel):
    id: int
    thumbnail_path: str | None


def purge_trash(conn: sqlite3.Connection, *, deleted_before: datetime | None) -> list[PurgedItem]:
    """휴지통 항목을 영구 정리한다. deleted_before 가 None 이면 휴지통 전부(비우기).

    자막·분석·임베딩·검색 색인·주제 연결·비슷한 영상·user_meta 를 지우고, items 행은
    status = 'deleted' 로 남긴다 (다시 가져올 때 건너뛰려고). 썸네일 파일은 부르는 쪽이 지운다.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.thumbnail_path
        FROM items AS i JOIN user_meta AS um ON um.item_id = i.id
        WHERE um.deleted_at IS NOT NULL AND i.status != 'deleted'
          AND (:before IS NULL OR um.deleted_at < :before)
        ORDER BY i.id
        """,
        {"before": to_iso(deleted_before) if deleted_before else None},
    ).fetchall()
    purged = [PurgedItem(**dict(r)) for r in rows]
    with conn:
        for p in purged:
            for sql in (
                "DELETE FROM transcripts WHERE item_id = ?",
                "DELETE FROM analyses WHERE item_id = ?",
                "DELETE FROM embeddings WHERE item_id = ?",
                "DELETE FROM items_fts WHERE rowid = ?",
                "DELETE FROM item_topics WHERE item_id = ?",
                "DELETE FROM user_meta WHERE item_id = ?",
            ):
                conn.execute(sql, (p.id,))
            conn.execute(
                "DELETE FROM item_similar WHERE item_id = ? OR similar_id = ?", (p.id, p.id)
            )
            conn.execute(
                "UPDATE items SET status = 'deleted', thumbnail_path = NULL, error = NULL"
                " WHERE id = ?",
                (p.id,),
            )
    return purged


def meta_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """설정 화면용: 숨긴 항목 수, 휴지통 항목 수."""
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(um.hidden_at IS NOT NULL AND um.deleted_at IS NULL), 0) AS hidden,
          COALESCE(SUM(um.deleted_at IS NOT NULL), 0) AS trash
        FROM user_meta AS um JOIN items AS i ON i.id = um.item_id
        WHERE i.status != 'deleted'
        """
    ).fetchone()
    return {"hidden": row["hidden"], "trash": row["trash"]}
