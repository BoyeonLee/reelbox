"""Phase 7: 즐겨찾기 탭 목록, 메모 검색 (005_note_search)."""

import shutil
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from reelbox import db
from reelbox.config import Settings
from reelbox.search import index as indexer
from reelbox.search import query as searcher

from .conftest import FakeEmbedder, analyzed_item, insert_item
from .test_web import AUTH, client, library, web_settings  # noqa: F401 (픽스처 재사용)


def _ids(body: dict) -> list[int]:
    return [c["id"] for c in body["items"]]


def _search(client: TestClient, q: str, **params: str) -> list[int]:  # noqa: F811
    return _ids(client.get("/api/search", params={"q": q, **params}, headers=AUTH).json())


# --- 즐겨찾기 탭 ---


def test_favorites_list(client: TestClient, library: dict[str, int]) -> None:  # noqa: F811
    for key in ("pasta", "stretch", "tomato"):
        client.patch(f"/api/items/{library[key]}", json={"favorite": True}, headers=AUTH)
    client.patch(f"/api/items/{library['tomato']}", json={"favorite": False}, headers=AUTH)
    client.patch(f"/api/items/{library['stretch']}", json={"hidden": True}, headers=AUTH)

    body = client.get("/api/items?favorite=true", headers=AUTH).json()
    # 즐겨찾기를 끈 것과 숨긴 것은 빠진다
    assert _ids(body) == [library["pasta"]]
    assert body["items"][0]["favorite"] is True


def test_favorites_paging_recent_first(client: TestClient, library: dict[str, int]) -> None:  # noqa: F811
    for key in ("pasta", "tomato", "stretch"):
        client.patch(f"/api/items/{library[key]}", json={"favorite": True}, headers=AUTH)
    first = client.get("/api/items?favorite=true&limit=2", headers=AUTH).json()
    assert _ids(first) == [library["stretch"], library["tomato"]]
    assert first["next_offset"] == 2
    rest = client.get("/api/items?favorite=true&limit=2&offset=2", headers=AUTH).json()
    assert (_ids(rest), rest["next_offset"]) == ([library["pasta"]], None)


def test_no_favorites(client: TestClient, library: dict[str, int]) -> None:  # noqa: F811
    assert _ids(client.get("/api/items?favorite=true", headers=AUTH).json()) == []
    # favorite 를 안 주면 전체 목록 그대로
    assert len(_ids(client.get("/api/items", headers=AUTH).json())) == 3


# --- 메모 검색 ---


def test_note_is_searchable(client: TestClient, library: dict[str, int]) -> None:  # noqa: F811
    tomato = library["tomato"]
    assert _search(client, "브런치") == []
    client.patch(f"/api/items/{tomato}", json={"note": "브런치 메뉴로 좋음"}, headers=AUTH)

    assert _search(client, "브런치") == [tomato]  # 3글자 이상: FTS5
    assert _search(client, "메뉴") == [tomato]  # 2글자: LIKE
    assert _search(client, "브런치", favorite="true") == []  # 다른 필터와 함께

    client.patch(f"/api/items/{tomato}", json={"note": ""}, headers=AUTH)
    assert _search(client, "브런치") == []
    assert _search(client, "메뉴") == []


def test_note_ranks_like_title(client: TestClient, library: dict[str, int]) -> None:  # noqa: F811
    # "스트레칭"은 stretch 의 제목에 있다. pasta 메모에도 넣으면 둘 다 나온다
    client.patch(f"/api/items/{library['pasta']}", json={"note": "먹고 스트레칭"}, headers=AUTH)
    assert set(_search(client, "스트레칭")) == {library["stretch"], library["pasta"]}


def test_note_written_before_index_is_indexed(conn: sqlite3.Connection, settings: Settings) -> None:
    item = analyzed_item(conn, "aaaaaaaaaaa", title="토마토 파스타")
    db.set_note(conn, [item], "캠핑 가서 해 먹기")  # 아직 색인 전 (items_fts 에 없음)
    indexer.index_items(conn, settings, FakeEmbedder())

    hits = searcher.search(conn, "캠핑 가서", embedder=None)
    assert [h.id for h in hits] == [item]


def test_migration_005_keeps_index_and_adds_notes(tmp_path: Path, settings: Settings) -> None:
    """004 까지 적용된 DB(기존 색인 + 메모)에 005 를 적용해도 다시 색인할 필요가 없다."""
    upto4 = tmp_path / "upto4"
    upto4.mkdir()
    for path in sorted(settings.migrations_dir.glob("00[1-4]_*.sql")):
        shutil.copy(path, upto4)
    conn = db.connect(tmp_path / "old.db")
    db.migrate(conn, upto4)
    item = insert_item(conn, "aaaaaaaaaaa", title="브라운버터 파스타", status="indexed")
    conn.execute(
        "INSERT INTO items_fts (rowid, title, description, transcript, summary, tags)"
        " VALUES (?, '브라운버터 파스타', '', '', '', '')",
        (item,),
    )
    conn.execute("INSERT INTO user_meta (item_id, note) VALUES (?, '주말 브런치')", (item,))
    conn.commit()

    assert db.migrate(conn, settings.migrations_dir) == ["005_note_search.sql"]

    assert [h.id for h in searcher.search(conn, "브라운버터", embedder=None)] == [item]
    assert [h.id for h in searcher.search(conn, "브런치", embedder=None)] == [item]
    conn.close()
