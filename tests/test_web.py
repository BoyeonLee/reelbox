"""웹 서버: 토큰 인증, JSON API, 정적 화면 (DESIGN.md 6장)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from reelbox import db
from reelbox.config import Settings
from reelbox.search import index as indexer
from reelbox.web.app import create_app
from reelbox.web.auth import COOKIE_NAME
from reelbox.worker import WorkerState

from .conftest import FakeEmbedder, analyzed_item

TOKEN = "test-token-1234567890"
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def web_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"token": SecretStr(TOKEN)})


@pytest.fixture
def library(conn: sqlite3.Connection, web_settings: Settings) -> dict[str, int]:
    ids = {
        "pasta": analyzed_item(
            conn,
            "aaaaaaaaaaa",
            title="브라운버터 파스타",
            summary="버터를 갈색으로 녹여 만드는 파스타",
            tags=["파스타", "브라운버터"],
            saved_at="2026-10-01T00:00:00Z",
            thumbnail_path="thumbs/youtube_aaaaaaaaaaa.webp",
        ),
        "tomato": analyzed_item(
            conn,
            "bbbbbbbbbbb",
            title="토마토 파스타",
            summary="생토마토 파스타",
            saved_at="2026-10-02T00:00:00Z",
        ),
        "stretch": analyzed_item(
            conn,
            "ccccccccccc",
            platform="instagram",
            url="https://www.instagram.com/reel/ccccccccccc/",
            title="허리 스트레칭",
            summary="아침 스트레칭",
            saved_at="2026-10-03T00:00:00Z",
        ),
    }
    indexer.index_items(conn, web_settings, FakeEmbedder())
    cook = conn.execute("INSERT INTO topics (name) VALUES ('요리')").lastrowid
    gym = conn.execute("INSERT INTO topics (name) VALUES ('운동')").lastrowid
    conn.executemany(
        "INSERT INTO item_topics (item_id, topic_id, is_primary) VALUES (?, ?, 1)",
        [(ids["pasta"], cook), (ids["tomato"], cook), (ids["stretch"], gym)],
    )
    conn.commit()
    ids["cook"], ids["gym"] = cook, gym  # type: ignore[assignment]
    return ids


@pytest.fixture
def client(web_settings: Settings, conn: sqlite3.Connection) -> Iterator[TestClient]:
    app = create_app(web_settings, auto_purge=False, now=lambda: NOW)
    with TestClient(app) as c:
        yield c


# --- 인증 ---


def test_app_refuses_to_start_without_token(settings: Settings) -> None:
    with pytest.raises(ValueError, match="REELBOX_TOKEN"):
        create_app(settings)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/home"),
        ("GET", "/api/search?q=x"),
        ("GET", "/api/items/1"),
        ("DELETE", "/api/items/1"),
        ("POST", "/api/trash/empty"),
        ("GET", "/thumbs/youtube_aaaaaaaaaaa.webp"),
    ],
)
def test_api_requires_token(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_header_token_works(client: TestClient, library: dict[str, int]) -> None:
    assert client.get("/api/home", headers=AUTH).status_code == 200


def test_login_sets_cookie_then_cookie_works(client: TestClient, library: dict[str, int]) -> None:
    assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
    res = client.post("/api/login", json={"token": TOKEN})
    assert res.status_code == 200
    set_cookie = res.headers["set-cookie"]
    assert COOKIE_NAME in set_cookie and "HttpOnly" in set_cookie
    assert client.get("/api/home").status_code == 200  # TestClient 가 쿠키를 들고 다닌다
    client.post("/api/logout")
    assert client.get("/api/home").status_code == 401


def test_cookie_is_secure_behind_https_proxy(client: TestClient) -> None:
    res = client.post("/api/login", json={"token": TOKEN}, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in res.headers["set-cookie"]


def test_static_shell_is_public(client: TestClient) -> None:
    for path in ("/", "/manifest.json", "/sw.js", "/data.js", "/app.js"):
        assert client.get(path).status_code == 200, path


def test_thumbs_served_and_path_traversal_blocked(
    client: TestClient, web_settings: Settings
) -> None:
    web_settings.thumbs_dir.mkdir(parents=True, exist_ok=True)
    (web_settings.thumbs_dir / "youtube_x.webp").write_bytes(b"RIFF")
    assert client.get("/thumbs/youtube_x.webp", headers=AUTH).content == b"RIFF"
    assert client.get("/thumbs/..%2F.env", headers=AUTH).status_code == 404
    assert client.get("/thumbs/missing.webp", headers=AUTH).status_code == 404


# --- 조회 ---


def test_home_lists_topics_and_recent(client: TestClient, library: dict[str, int]) -> None:
    body = client.get("/api/home", headers=AUTH).json()
    assert body["total"] == 3
    assert [(t["name"], t["count"]) for t in body["topics"]] == [("요리", 2), ("운동", 1)]
    assert body["topics"][0]["thumbs"] == ["/thumbs/youtube_aaaaaaaaaaa.webp"]
    assert [c["id"] for c in body["recent"]] == [
        library["stretch"],
        library["tomato"],
        library["pasta"],
    ]
    assert body["recent"][0]["topic"] == "운동"


def test_topic_items_sort_and_paging(client: TestClient, library: dict[str, int]) -> None:
    cook = library["cook"]
    first = client.get(f"/api/items?topic_id={cook}&limit=1", headers=AUTH).json()
    assert [c["id"] for c in first["items"]] == [library["tomato"]]
    assert first["next_offset"] == 1
    oldest = client.get(f"/api/items?topic_id={cook}&sort=oldest", headers=AUTH).json()
    assert [c["id"] for c in oldest["items"]] == [library["pasta"], library["tomato"]]
    assert oldest["next_offset"] is None
    topic = client.get(f"/api/topics/{cook}", headers=AUTH).json()
    assert (topic["name"], topic["count"]) == ("요리", 2)


def test_item_detail(client: TestClient, library: dict[str, int]) -> None:
    body = client.get(f"/api/items/{library['pasta']}", headers=AUTH).json()
    assert body["title"] == "브라운버터 파스타"
    assert body["tags"] == ["파스타", "브라운버터"]
    assert body["url"] == "https://www.youtube.com/shorts/aaaaaaaaaaa"
    assert body["hidden"] is False
    assert library["tomato"] in [s["id"] for s in body["similar"]]
    assert client.get("/api/items/999", headers=AUTH).status_code == 404


def test_search_keyword_only_and_filters(client: TestClient, library: dict[str, int]) -> None:
    body = client.get("/api/search", params={"q": "파스타"}, headers=AUTH).json()
    assert body["semantic"] is False
    assert {c["id"] for c in body["items"]} == {library["pasta"], library["tomato"]}
    insta = client.get(
        "/api/search", params={"q": "스트레칭", "platform": "instagram"}, headers=AUTH
    ).json()
    assert [c["id"] for c in insta["items"]] == [library["stretch"]]
    since = client.get(
        "/api/search", params={"q": "파스타", "since": "2026-10-02"}, headers=AUTH
    ).json()
    assert [c["id"] for c in since["items"]] == [library["tomato"]]


def test_search_uses_embedder_when_ready(
    web_settings: Settings, conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    app = create_app(web_settings, auto_purge=False, embedder_factory=FakeEmbedder)
    with TestClient(app) as c:
        holder = app.state.embedder
        for _ in range(100):  # 백그라운드 스레드가 끝나기를 잠깐 기다린다
            if holder.get() is not None:
                break
            import time

            time.sleep(0.01)
        body = c.get("/api/search", params={"q": "버터 녹이기"}, headers=AUTH).json()
    assert body["semantic"] is True
    assert body["items"][0]["id"] == library["pasta"]


# --- 숨기기·삭제·복원 ---


def test_hide_delete_restore_flow(client: TestClient, library: dict[str, int]) -> None:
    pasta, tomato = library["pasta"], library["tomato"]
    assert client.patch(f"/api/items/{pasta}", json={"hidden": True}, headers=AUTH).json() == {
        "ids": [pasta]
    }
    assert client.delete(f"/api/items/{tomato}", headers=AUTH).status_code == 200

    home = client.get("/api/home", headers=AUTH).json()
    assert [c["id"] for c in home["recent"]] == [library["stretch"]]
    assert [t["name"] for t in home["topics"]] == ["운동"]  # 요리는 0개라 감춤
    search = client.get("/api/search", params={"q": "파스타"}, headers=AUTH).json()
    assert search["items"] == []

    hidden = client.get("/api/hidden", headers=AUTH).json()
    assert [c["id"] for c in hidden] == [pasta]
    trash = client.get("/api/trash", headers=AUTH).json()
    assert [c["id"] for c in trash] == [tomato]
    assert trash[0]["purge_at"] == "2026-12-08T12:00:00Z"
    # 숨김·휴지통 항목도 상세는 열린다 (되돌리기용)
    assert client.get(f"/api/items/{pasta}", headers=AUTH).json()["hidden"] is True

    client.patch(f"/api/items/{pasta}", json={"hidden": False}, headers=AUTH)
    assert client.post(f"/api/items/{tomato}/restore", headers=AUTH).json() == {"ids": [tomato]}
    assert client.get("/api/home", headers=AUTH).json()["total"] == 3

    status = client.get("/api/status", headers=AUTH).json()
    assert (status["hidden"], status["trash"], status["total"]) == (0, 0, 3)


def test_missing_item_returns_404(client: TestClient, library: dict[str, int]) -> None:
    assert client.delete("/api/items/999", headers=AUTH).status_code == 404
    assert client.patch("/api/items/999", json={"hidden": True}, headers=AUTH).status_code == 404


def test_bulk_actions(client: TestClient, library: dict[str, int]) -> None:
    ids = [library["pasta"], library["tomato"], 999]
    res = client.post("/api/items/bulk", json={"ids": ids, "action": "delete"}, headers=AUTH)
    assert res.json() == {"ids": ids[:2]}
    assert client.get("/api/status", headers=AUTH).json()["trash"] == 2
    res = client.post("/api/items/bulk", json={"ids": ids, "action": "restore"}, headers=AUTH)
    assert res.json() == {"ids": ids[:2]}
    res = client.post("/api/items/bulk", json={"ids": ids, "action": "hide"}, headers=AUTH)
    assert client.get("/api/status", headers=AUTH).json()["hidden"] == 2
    bad = client.post("/api/items/bulk", json={"ids": ids, "action": "nuke"}, headers=AUTH)
    assert bad.status_code == 422


def test_empty_trash(client: TestClient, library: dict[str, int], conn: sqlite3.Connection) -> None:
    client.delete(f"/api/items/{library['pasta']}", headers=AUTH)
    assert client.post("/api/trash/empty", headers=AUTH).json() == {"ids": [library["pasta"]]}
    assert client.get("/api/trash", headers=AUTH).json() == []
    assert client.get(f"/api/items/{library['pasta']}", headers=AUTH).status_code == 404
    status = conn.execute("SELECT status FROM items WHERE id = ?", (library["pasta"],)).fetchone()
    assert status[0] == "deleted"


# --- 추가 ---


def test_add_new_duplicate_and_unrecognized(client: TestClient, library: dict[str, int]) -> None:
    new = client.post(
        "/api/items", json={"url": "https://youtu.be/zzzzzzzzzzz?si=abc"}, headers=AUTH
    ).json()
    assert new["result"] == "added"
    assert new["item"]["status"] == "imported"
    assert new["item"]["url"] == "https://www.youtube.com/shorts/zzzzzzzzzzz"
    dup = client.post(
        "/api/items", json={"url": "https://www.youtube.com/shorts/aaaaaaaaaaa"}, headers=AUTH
    ).json()
    assert dup["result"] == "duplicate"
    bad = client.post("/api/items", json={"url": "https://example.com"}, headers=AUTH).json()
    assert (bad["result"], bad["item"]) == ("unrecognized", None)


def test_add_restores_trashed_and_skips_purged(client: TestClient, library: dict[str, int]) -> None:
    client.delete(f"/api/items/{library['pasta']}", headers=AUTH)
    url = {"url": "https://www.youtube.com/shorts/aaaaaaaaaaa"}
    restored = client.post("/api/items", json=url, headers=AUTH).json()
    assert restored["result"] == "restored"
    assert client.get("/api/trash", headers=AUTH).json() == []

    client.delete(f"/api/items/{library['pasta']}", headers=AUTH)
    client.post("/api/trash/empty", headers=AUTH)
    skipped = client.post("/api/items", json=url, headers=AUTH).json()
    assert (skipped["result"], skipped["item"]) == ("deleted", None)


def test_recent_adds(client: TestClient) -> None:
    client.post("/api/items", json={"url": "https://youtu.be/zzzzzzzzzzz"}, headers=AUTH)
    adds = client.get("/api/adds", headers=AUTH).json()
    assert [c["url"] for c in adds] == ["https://www.youtube.com/shorts/zzzzzzzzzzz"]


def test_detail_of_unanalyzed_item(client: TestClient, conn: sqlite3.Connection) -> None:
    db.add_item(conn, "https://youtu.be/yyyyyyyyyyy", saved_at=None, source="share")
    conn.commit()
    item_id = db.find_item_id(conn, "youtube", "yyyyyyyyyyy")
    body = client.get(f"/api/items/{item_id}", headers=AUTH).json()
    assert (body["status"], body["tags"], body["key_points"]) == ("imported", [], [])


# --- Phase 6: 공유 처리 워커 ---


class SpyWorker:
    """워커 대신. notify 호출만 센다."""

    def __init__(self) -> None:
        self.notified = 0
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def notify(self) -> None:
        self.notified += 1

    def state(self) -> WorkerState:
        return WorkerState(busy=True, step="분석", pending=1)


def test_worker_state_when_disabled(client: TestClient) -> None:
    assert client.get("/api/worker", headers=AUTH).json()["enabled"] is False


def test_add_wakes_worker_and_reports_state(
    web_settings: Settings, conn: sqlite3.Connection
) -> None:
    spy = SpyWorker()
    app = create_app(web_settings, auto_purge=False, now=lambda: NOW, make_worker=lambda _: spy)
    with TestClient(app) as c:
        assert spy.started
        url = {"url": "https://youtu.be/zzzzzzzzzzz"}
        c.post("/api/items", json=url, headers=AUTH)
        c.post("/api/items", json=url, headers=AUTH)  # 중복은 깨우지 않음
        assert spy.notified == 1
        state = c.get("/api/worker", headers=AUTH).json()
        assert (state["enabled"], state["busy"], state["step"]) == (True, True, "분석")
    assert spy.stopped


def test_add_picks_url_out_of_shared_text(client: TestClient) -> None:
    text = "이 릴스 보세요! https://www.instagram.com/reel/AbCdEfGhIjK/?igsh=xyz 진짜 맛있음"
    body = client.post("/api/items", json={"url": text}, headers=AUTH).json()
    assert body["result"] == "added"
    assert body["item"]["url"] == "https://www.instagram.com/reel/AbCdEfGhIjK/"


def test_static_shell_is_revalidated(client: TestClient) -> None:
    # 새 index.html 과 옛 app.js 가 섞이지 않게 화면 파일은 매번 바뀌었는지 확인시킨다
    for path in ["/", "/app.js", "/data.js", "/sw.js"]:
        assert client.get(path).headers["cache-control"] == "no-cache"


# --- 아이폰 단축어가 보내는 모양 ---


def test_add_accepts_list_from_shortcut(client: TestClient) -> None:
    # 공유 항목이 여러 개면 단축어는 url 을 목록으로 보낸다 (제목 글 + 링크)
    body = {"url": ["브라운버터 파스타 #shorts", "https://youtube.com/shorts/SggDZVu0DPk?si=abc"]}
    res = client.post("/api/items", json=body, headers=AUTH).json()
    assert res["result"] == "added"
    assert res["item"]["url"] == "https://www.youtube.com/shorts/SggDZVu0DPk"


def test_errors_carry_message_for_shortcut_notification(client: TestClient) -> None:
    bad_token = client.post(
        "/api/items", json={"url": "https://youtu.be/zzzzzzzzzzz"}, headers={"Authorization": "x"}
    )
    assert bad_token.status_code == 401
    assert "토큰" in bad_token.json()["message"]
    assert bad_token.json()["detail"] == "토큰이 필요합니다"  # 화면(data.js)은 detail 을 읽는다

    bad_body = client.post(
        "/api/items", json={"link": "https://youtu.be/zzzzzzzzzzz"}, headers=AUTH
    )
    assert bad_body.status_code == 422
    assert "url" in bad_body.json()["message"]


def test_add_finds_url_when_shortcut_key_is_wrong(client: TestClient) -> None:
    # 단축어 키 칸이 비고 값에 "url" 글자 + 링크가 들어간 실제 사례
    body = {"": "url https://www.instagram.com/reel/AbCdEfGhIjK/?igsh=xyz"}
    res = client.post("/api/items", json=body, headers=AUTH).json()
    assert res["result"] == "added"
    assert res["item"]["url"] == "https://www.instagram.com/reel/AbCdEfGhIjK/"
