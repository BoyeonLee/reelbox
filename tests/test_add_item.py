import sqlite3
from datetime import UTC, datetime

from reelbox import db
from reelbox.db import AddOutcome

from .conftest import insert_item

T1 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM items ORDER BY id").fetchall()


def test_add_new_item(conn: sqlite3.Connection) -> None:
    out = db.add_item(
        conn,
        "https://youtu.be/dQw4w9WgXcQ?si=x",
        saved_at=T1,
        source="youtube_sync",
        collection=None,
    )
    assert out is AddOutcome.ADDED
    [row] = _rows(conn)
    assert row["platform"] == "youtube"
    assert row["source_id"] == "dQw4w9WgXcQ"
    assert row["url"] == "https://www.youtube.com/shorts/dQw4w9WgXcQ"
    assert row["saved_at"] == "2026-10-01T12:00:00Z"
    assert row["imported_from"] == "youtube_sync"
    assert row["status"] == "imported"
    assert row["media_type"] == "video"


def test_different_url_forms_are_one_item(conn: sqlite3.Connection) -> None:
    forms = [
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=3",
    ]
    outcomes = [db.add_item(conn, u, saved_at=T1, source="urllist") for u in forms]
    assert outcomes == [AddOutcome.ADDED, AddOutcome.DUPLICATE, AddOutcome.DUPLICATE]
    assert len(_rows(conn)) == 1


def test_duplicate_keeps_existing_values_and_fills_blanks(conn: sqlite3.Connection) -> None:
    url = "https://www.instagram.com/reel/DAbc_12-xyZ/"
    assert db.add_item(conn, url, saved_at=None, source="urllist") is AddOutcome.ADDED
    out = db.add_item(conn, url, saved_at=T1, source="instagram_export", collection="요리")
    assert out is AddOutcome.DUPLICATE
    [row] = _rows(conn)
    assert row["saved_at"] == "2026-10-01T12:00:00Z"
    assert row["collection"] == "요리"
    assert row["imported_from"] == "urllist"  # 처음 들어온 입구는 그대로

    # 이미 값이 있으면 덮어쓰지 않는다
    db.add_item(conn, url, saved_at=T2, source="instagram_export", collection="다른 것")
    [row] = _rows(conn)
    assert row["saved_at"] == "2026-10-01T12:00:00Z"
    assert row["collection"] == "요리"


def test_deleted_item_is_skipped(conn: sqlite3.Connection) -> None:
    insert_item(conn, "dQw4w9WgXcQ", status="deleted")
    out = db.add_item(conn, "https://youtu.be/dQw4w9WgXcQ", saved_at=T1, source="urllist")
    assert out is AddOutcome.DELETED
    [row] = _rows(conn)
    assert row["status"] == "deleted"
    assert row["saved_at"] is None


def test_unrecognized_url(conn: sqlite3.Connection) -> None:
    out = db.add_item(conn, "https://example.com/x", saved_at=T1, source="urllist")
    assert out is AddOutcome.UNRECOGNIZED
    assert _rows(conn) == []
