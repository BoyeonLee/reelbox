"""임베딩 텍스트, 색인(index), 비슷한 영상. 모델 대신 가짜 Embedder."""

import sqlite3

import numpy as np

from reelbox import db
from reelbox.config import Settings
from reelbox.search import index as indexer
from reelbox.search.embed import encode_batched, item_text

from .conftest import FakeEmbedder, analyzed_item, set_user_meta


def _input(**cols: object) -> db.EmbedInput:
    base: dict[str, object] = {
        "id": 1,
        "title": None,
        "description": None,
        "transcript_text": None,
        "summary_ko": None,
        "raw_topic": None,
        "tags": [],
    }
    return db.EmbedInput(**{**base, **cols})


def test_item_text_uses_title_summary_tags_and_transcript_head() -> None:
    text = item_text(
        _input(
            title="김치찌개",
            summary_ko="3분 레시피",
            tags=["찌개", "자취"],
            transcript_text="  가나다라마바사  ",
            description="설명은 안 넣음",
        ),
        transcript_chars=4,
    )
    assert text == "김치찌개\n3분 레시피\n태그: 찌개, 자취\n가나다라"


def test_item_text_skips_empty_parts() -> None:
    assert item_text(_input(title="제목만"), transcript_chars=100) == "제목만"


def test_encode_batched_reports_progress() -> None:
    embedder = FakeEmbedder()
    progress: list[tuple[int, int]] = []
    out = encode_batched(
        embedder, ["가나", "다라", "마바"], 2, lambda d, t: progress.append((d, t))
    )
    assert out.shape == (3, 1024)
    assert progress == [(2, 3), (3, 3)]
    assert [len(c) for c in embedder.calls] == [2, 1]


def test_vector_blob_roundtrip() -> None:
    vec = [0.5, -1.25, 3.0]
    assert db.blob_to_vector(db.vector_to_blob(vec)) == vec


def test_migration_creates_item_similar(conn: sqlite3.Connection) -> None:
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "item_similar" in names


def _fts_row(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items_fts WHERE rowid = ?", (item_id,)).fetchone()


def test_index_fills_fts_and_vectors_and_marks_indexed(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    a = analyzed_item(
        conn, "aaaaaaaaaaa", title="김치찌개", summary="찌개 레시피", tags=["찌개", "한식"]
    )
    b = analyzed_item(conn, "bbbbbbbbbbb", title="파스타", transcript="면을 삶는다")
    enriched = analyzed_item(conn, "ccccccccccc", title="아직", status="enriched")

    report = indexer.index_items(conn, settings, FakeEmbedder())

    assert (report.indexed, report.embedded, report.reused) == (2, 2, 0)
    row = _fts_row(conn, a)
    assert row is not None
    assert (row["title"], row["summary"], row["tags"]) == ("김치찌개", "찌개 레시피", "찌개 한식")
    assert _fts_row(conn, b)["transcript"] == "면을 삶는다"
    assert _fts_row(conn, enriched) is None
    assert set(db.load_vectors(conn, [a, b, enriched])) == {a, b}
    statuses = dict(conn.execute("SELECT id, status FROM items").fetchall())
    assert statuses == {a: "indexed", b: "indexed", enriched: "enriched"}


def test_index_is_idempotent(conn: sqlite3.Connection, settings: Settings) -> None:
    analyzed_item(conn, "aaaaaaaaaaa", title="김치찌개")
    embedder = FakeEmbedder()
    indexer.index_items(conn, settings, embedder)
    again = indexer.index_items(conn, settings, embedder)
    assert again.indexed == 0
    assert embedder.encoded == 1
    assert conn.execute("SELECT COUNT(*) FROM items_fts").fetchone()[0] == 1


def test_index_limit(conn: sqlite3.Connection, settings: Settings) -> None:
    for sid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"):
        analyzed_item(conn, sid, title=sid)
    report = indexer.index_items(conn, settings, FakeEmbedder(), limit=2)
    assert report.indexed == 2
    assert db.status_counts(conn)["analyzed"] == 1


def test_index_reuses_vectors_saved_by_organize(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    a = analyzed_item(conn, "aaaaaaaaaaa", title="김치찌개")
    analyzed_item(conn, "bbbbbbbbbbb", title="파스타")
    db.save_vectors(conn, {a: [1.0] + [0.0] * 1023})
    embedder = FakeEmbedder()
    report = indexer.index_items(conn, settings, embedder)
    assert (report.embedded, report.reused) == (1, 1)
    assert embedder.encoded == 1
    assert db.load_vectors(conn, [a])[a][0] == 1.0


def test_top_similar_picks_nearest_and_skips_self() -> None:
    m = np.array([[1, 0], [0.9, 0.1], [0, 1]], dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    rows = indexer.top_similar([10, 20, 30], m, k=1)
    assert [(r[0], r[1], r[2]) for r in rows] == [(10, 20, 1), (20, 10, 1), (30, 20, 1)]
    assert indexer.top_similar([10], m[:1], k=5) == []


def test_similar_is_computed_and_excludes_hidden_and_deleted(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    settings.similar_count = 5
    a = analyzed_item(conn, "aaaaaaaaaaa", title="김치찌개 끓이는 법")
    b = analyzed_item(conn, "bbbbbbbbbbb", title="김치찌개 맛집")
    c = analyzed_item(conn, "ccccccccccc", title="파스타")
    hidden = analyzed_item(conn, "ddddddddddd", title="김치찌개 끓이는 법 2")
    trashed = analyzed_item(conn, "eeeeeeeeeee", title="김치찌개 끓이는 법 3")
    set_user_meta(conn, hidden, hidden_at="2026-11-01T00:00:00Z")
    set_user_meta(conn, trashed, deleted_at="2026-11-01T00:00:00Z")

    report = indexer.index_items(conn, settings, FakeEmbedder())

    assert report.similar_items == 3
    assert db.similar_ids(conn, a) == [b, c]  # 숨김·삭제 빼고, 가까운 순
    assert db.similar_ids(conn, hidden) == []


def test_similar_is_refreshed_even_without_new_items(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    a = analyzed_item(conn, "aaaaaaaaaaa", title="김치찌개")
    b = analyzed_item(conn, "bbbbbbbbbbb", title="김치찌개 맛집")
    indexer.index_items(conn, settings, FakeEmbedder())
    assert db.similar_ids(conn, a) == [b]
    set_user_meta(conn, b, hidden_at="2026-11-01T00:00:00Z")
    indexer.index_items(conn, settings, FakeEmbedder())
    assert db.similar_ids(conn, a) == []
