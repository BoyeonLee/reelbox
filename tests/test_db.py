import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

from reelbox import db
from reelbox.config import Settings

from .conftest import insert_item

EXPECTED_TABLES = {
    "items",
    "transcripts",
    "analyses",
    "topics",
    "item_topics",
    "embeddings",
    "items_fts",
    "user_meta",
}


def test_init_creates_all_tables(settings: Settings) -> None:
    result = db.init_db(settings)
    assert result.applied == ["001_init.sql", "002_fetch_log.sql", "003_similar.sql"]
    assert result.version == 3
    assert settings.db_path.is_file()

    conn = db.connect(settings.db_path)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert EXPECTED_TABLES <= names


def test_init_is_idempotent(settings: Settings) -> None:
    db.init_db(settings)
    again = db.init_db(settings)
    assert again.applied == []
    assert again.version == 3


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_ok.sql").write_text("CREATE TABLE a (x INTEGER);")
    (migrations / "002_bad.sql").write_text("CREATE TABLE b (x INTEGER);\nNOT VALID SQL;")

    conn = db.connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.Error):
        db.migrate(conn, migrations)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert db.schema_version(conn) == 1
    assert "a" in names
    assert "b" not in names  # 002 전체가 되돌려짐
    conn.close()


def test_platform_source_id_unique(conn: sqlite3.Connection) -> None:
    insert_item(conn, "abc123")
    with pytest.raises(sqlite3.IntegrityError):
        insert_item(conn, "abc123")
    # 플랫폼이 다르면 같은 ID 라도 허용
    insert_item(conn, "abc123", platform="instagram", url="https://www.instagram.com/reel/abc123/")


def test_invalid_status_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_item(conn, "x1", status="done")


def test_updated_at_trigger(conn: sqlite3.Connection) -> None:
    item_id = insert_item(conn, "t1", updated_at="2000-01-01T00:00:00Z")
    conn.execute("UPDATE items SET title = 'new' WHERE id = ?", (item_id,))
    row = conn.execute("SELECT updated_at FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["updated_at"] != "2000-01-01T00:00:00Z"


def test_fts_trigram_korean_substring(conn: sqlite3.Connection) -> None:
    item_id = insert_item(conn, "fts1")
    conn.execute(
        "INSERT INTO items_fts (rowid, title, summary) VALUES (?, ?, ?)",
        (item_id, "브라운버터파스타 만들기", "자취생도 쉽게 만드는 파스타"),
    )
    rows = conn.execute(
        "SELECT rowid FROM items_fts WHERE items_fts MATCH ?", ("버터파",)
    ).fetchall()
    assert [r["rowid"] for r in rows] == [item_id]
    none = conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ?", ("김치찌개",))
    assert none.fetchall() == []


def test_vec_knn(conn: sqlite3.Connection) -> None:
    a = insert_item(conn, "v1")
    b = insert_item(conn, "v2")
    va = [1.0] + [0.0] * 1023
    vb = [0.0, 1.0] + [0.0] * 1022
    for item_id, vec in ((a, va), (b, vb)):
        conn.execute(
            "INSERT INTO embeddings (item_id, vector) VALUES (?, ?)",
            (item_id, sqlite_vec.serialize_float32(vec)),
        )
    rows = conn.execute(
        "SELECT item_id FROM embeddings WHERE vector MATCH ? AND k = 2 ORDER BY distance",
        (sqlite_vec.serialize_float32(va),),
    ).fetchall()
    assert [r["item_id"] for r in rows] == [a, b]


def test_status_counts_and_recent_failures(conn: sqlite3.Connection) -> None:
    insert_item(conn, "s1")
    insert_item(conn, "s2")
    insert_item(conn, "s3", status="indexed")
    failed = insert_item(conn, "s4", status="failed", error="429 Too Many Requests", attempts=2)

    counts = db.status_counts(conn)
    assert list(counts) == list(db.ITEM_STATUSES)
    assert counts["imported"] == 2
    assert counts["indexed"] == 1
    assert counts["failed"] == 1
    assert counts["skipped"] == 0

    failures = db.recent_failures(conn)
    assert len(failures) == 1
    assert failures[0].id == failed
    assert failures[0].error == "429 Too Many Requests"
    assert failures[0].attempts == 2
