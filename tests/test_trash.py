"""숨기기·삭제(휴지통)·복원·purge (DESIGN.md 4장 "숨기기와 삭제")."""

import sqlite3
from datetime import UTC, datetime, timedelta

from reelbox import db
from reelbox.config import Settings
from reelbox.db import AddOutcome
from reelbox.purge import purge
from reelbox.search import index as indexer
from reelbox.search import query as searcher

from .conftest import FakeEmbedder, analyzed_item

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _topic(conn: sqlite3.Connection, name: str, *item_ids: int) -> int:
    topic_id = conn.execute("INSERT INTO topics (name) VALUES (?)", (name,)).lastrowid
    for item_id in item_ids:
        conn.execute(
            "INSERT INTO item_topics (item_id, topic_id, is_primary) VALUES (?, ?, 1)",
            (item_id, topic_id),
        )
    conn.commit()
    return topic_id  # type: ignore[return-value]


def _library(conn: sqlite3.Connection, settings: Settings) -> tuple[int, int, int]:
    a = analyzed_item(conn, "aaaaaaaaaaa", title="브라운버터 파스타", saved_at="2026-10-01")
    b = analyzed_item(conn, "bbbbbbbbbbb", title="토마토 파스타", saved_at="2026-10-02")
    c = analyzed_item(conn, "ccccccccccc", title="허리 스트레칭", saved_at="2026-10-03")
    indexer.index_items(conn, settings, FakeEmbedder())
    return a, b, c


def test_hidden_and_trashed_items_leave_lists_search_and_topics(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    a, b, c = _library(conn, settings)
    topic = _topic(conn, "요리", a, b)
    _topic(conn, "운동", c)

    assert db.set_hidden(conn, [a], True, now=NOW) == [a]
    assert db.trash_items(conn, [c], now=NOW) == [c]

    assert [i.id for i in db.list_items(conn)] == [b]
    assert [h.id for h in searcher.search(conn, "파스타", embedder=None)] == [b]
    cards = db.topic_cards(conn)
    # 운동 주제는 남은 영상이 0개라 카드에서 빠진다
    assert [(t.id, t.items) for t in cards] == [(topic, 1)]
    assert [i.id for i in db.list_hidden(conn)] == [a]
    assert [i.id for i in db.list_trash(conn)] == [c]
    assert db.meta_counts(conn) == {"hidden": 1, "trash": 1}


def test_unhide_and_restore_bring_items_back(conn: sqlite3.Connection, settings: Settings) -> None:
    a, b, c = _library(conn, settings)
    db.set_hidden(conn, [a], True, now=NOW)
    db.trash_items(conn, [b], now=NOW)
    db.set_hidden(conn, [a], False, now=NOW)
    db.restore_items(conn, [b])
    assert {i.id for i in db.list_items(conn)} == {a, b, c}


def test_trashed_hidden_item_shows_only_in_trash(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    a, _, _ = _library(conn, settings)
    db.set_hidden(conn, [a], True, now=NOW)
    db.trash_items(conn, [a], now=NOW)
    assert db.list_hidden(conn) == []
    assert [i.id for i in db.list_trash(conn)] == [a]


def test_unknown_ids_are_ignored(conn: sqlite3.Connection, settings: Settings) -> None:
    a, _, _ = _library(conn, settings)
    assert db.trash_items(conn, [a, 999, a], now=NOW) == [a]


def test_purge_only_old_trash_and_keeps_items_row(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    a, b, c = _library(conn, settings)
    _topic(conn, "요리", a, b)
    thumb = settings.thumbs_dir / "youtube_aaaaaaaaaaa.webp"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"x")
    conn.execute(
        "UPDATE items SET thumbnail_path = 'thumbs/youtube_aaaaaaaaaaa.webp' WHERE id = ?", (a,)
    )
    conn.commit()
    db.trash_items(conn, [a], now=NOW - timedelta(days=31))
    db.trash_items(conn, [b], now=NOW - timedelta(days=5))

    assert purge(conn, settings, now=NOW) == [a]
    assert purge(conn, settings, now=NOW) == []  # 멱등

    row = conn.execute("SELECT status, thumbnail_path FROM items WHERE id = ?", (a,)).fetchone()
    assert (row["status"], row["thumbnail_path"]) == ("deleted", None)
    assert not thumb.exists()
    for table, col in [
        ("transcripts", "item_id"),
        ("analyses", "item_id"),
        ("embeddings", "item_id"),
        ("item_topics", "item_id"),
        ("user_meta", "item_id"),
        ("item_similar", "similar_id"),
        ("items_fts", "rowid"),
    ]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (a,)).fetchone()[0]
        assert count == 0, table
    # 5일 된 것은 휴지통에 그대로
    assert [i.id for i in db.list_trash(conn)] == [b]
    assert db.get_item_detail(conn, a) is None


def test_empty_trash_purges_everything(conn: sqlite3.Connection, settings: Settings) -> None:
    a, b, _ = _library(conn, settings)
    db.trash_items(conn, [a, b], now=NOW)
    assert purge(conn, settings, now=NOW, days=None) == [a, b]
    assert db.list_trash(conn) == []


def test_import_skips_trashed_and_purged_items(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    a, b, _ = _library(conn, settings)
    db.trash_items(conn, [a, b], now=NOW - timedelta(days=40))
    purge(conn, settings, now=NOW, days=35)  # 둘 다 정리됨
    url_a = "https://www.youtube.com/shorts/aaaaaaaaaaa"
    assert db.add_item(conn, url_a, saved_at=None, source="urllist") is AddOutcome.DELETED

    c = analyzed_item(conn, "ddddddddddd", title="휴지통 영상")
    db.trash_items(conn, [c], now=NOW)
    url_c = "https://youtu.be/ddddddddddd"
    assert db.add_item(conn, url_c, saved_at=None, source="urllist") is AddOutcome.DELETED
