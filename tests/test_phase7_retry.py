"""Phase 7: 실패한 항목 다시 시도 (DB·워커·API)."""

import sqlite3

from fastapi.testclient import TestClient

from reelbox import db

from .conftest import insert_item
from .test_enrich_runner import SUBS, yt_info
from .test_web import AUTH, client, web_settings  # noqa: F401 (픽스처 재사용)
from .test_worker import Setup, s  # noqa: F401


def test_retry_resets_attempts_only_for_failed(conn: sqlite3.Connection) -> None:
    failed = insert_item(conn, "aaaaaaaaaaa", status="failed", attempts=3, error="차단")
    ok = insert_item(conn, "bbbbbbbbbbb", status="indexed")

    assert db.retry_items(conn, [ok, failed, 999, failed]) == [failed]

    row = conn.execute("SELECT * FROM items WHERE id = ?", (failed,)).fetchone()
    assert (row["status"], row["attempts"], row["retry_requested"]) == ("failed", 0, 1)
    assert row["error"] == "차단"  # 마지막 실패 이유는 성공할 때까지 남긴다
    assert db.recent_failures(conn)[0].retry_requested is True


def test_worker_retries_exported_item_after_retry(s: Setup) -> None:  # noqa: F811
    # 내보내기로 들어와 3번 실패한 항목: 워커는 원래 손대지 않는다
    item = s.env.yt(
        "bbbbbbbbbbb",
        {**yt_info("bbbbbbbbbbb"), **SUBS},
        imported_from="instagram_export",
        status="failed",
        attempts=3,
    )
    s.worker().run_once()
    assert s.status(item) == "failed"
    assert s.env.extracted() == []

    db.retry_items(s.conn, [item])
    s.worker().run_once()

    assert s.status(item) == "indexed"
    assert s.topic_of(item) == "요리"


def test_worker_still_ignores_other_exported_items(s: Setup) -> None:  # noqa: F811
    retried = s.env.yt("aaaaaaaaaaa", imported_from="instagram_export", status="failed")
    untouched = s.env.yt("ccccccccccc", imported_from="instagram_export", status="failed")
    db.retry_items(s.conn, [retried])
    s.worker().run_once()
    assert "https://www.youtube.com/shorts/ccccccccccc" not in s.env.extracted()
    assert s.status(untouched) == "failed"
    assert "https://www.youtube.com/shorts/aaaaaaaaaaa" in s.env.extracted()


def test_retry_api(client: TestClient, conn: sqlite3.Connection) -> None:  # noqa: F811
    a = insert_item(conn, "aaaaaaaaaaa", status="failed", attempts=3, error="오류 A")
    b = insert_item(conn, "bbbbbbbbbbb", status="failed", attempts=1, error="오류 B")
    ok = insert_item(conn, "ccccccccccc", status="indexed")

    failed = client.get("/api/failed", headers=AUTH).json()
    assert {f["id"] for f in failed} == {a, b}
    assert all(f["retry_requested"] is False for f in failed)

    assert client.post(f"/api/items/{a}/retry", headers=AUTH).json() == {"ids": [a]}
    assert client.post(f"/api/items/{ok}/retry", headers=AUTH).status_code == 404
    assert client.post("/api/items/999/retry", headers=AUTH).status_code == 404

    assert sorted(client.post("/api/failed/retry", headers=AUTH).json()["ids"]) == [a, b]
    failed = client.get("/api/failed", headers=AUTH).json()
    assert all(f["retry_requested"] and f["attempts"] == 0 for f in failed)
