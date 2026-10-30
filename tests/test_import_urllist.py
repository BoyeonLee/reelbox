import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from reelbox.importers import ImportReport
from reelbox.importers.urllist import import_urls

URLS = Path(__file__).parent / "fixtures" / "urls.txt"
NOW = datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC)


def test_import_urls(conn: sqlite3.Connection) -> None:
    report = import_urls(conn, URLS, now=NOW)
    assert report == ImportReport(added=4, duplicate=1, unrecognized=1)
    rows = {r["source_id"]: r for r in conn.execute("SELECT * FROM items")}
    assert set(rows) == {"urlA0000001", "URLreel0002", "URLpost0003", "urlB0000004"}
    assert rows["URLreel0002"]["url"] == "https://www.instagram.com/reel/URLreel0002/"
    assert rows["urlB0000004"]["platform"] == "youtube"
    assert {r["imported_from"] for r in rows.values()} == {"urllist"}
    assert {r["saved_at"] for r in rows.values()} == {"2026-10-30T09:00:00Z"}


def test_reimport_adds_nothing(conn: sqlite3.Connection) -> None:
    import_urls(conn, URLS, now=NOW)
    assert import_urls(conn, URLS, now=NOW) == ImportReport(duplicate=5, unrecognized=1)


def test_limit_counts_link_lines(conn: sqlite3.Connection) -> None:
    # 주석·빈 줄은 세지 않는다
    assert import_urls(conn, URLS, limit=2, now=NOW) == ImportReport(added=1, duplicate=1)


def test_bom_and_crlf(conn: sqlite3.Connection, tmp_path: Path) -> None:
    f = tmp_path / "list.txt"
    f.write_bytes("﻿https://youtu.be/bomA0000001\r\nhttps://youtu.be/bomB0000002\r\n".encode())
    assert import_urls(conn, f, now=NOW) == ImportReport(added=2)
