"""Phase 7: 주제 이름 바꾸기·합치기, 영상 주제 바꾸기, 즐겨찾기·메모 (DB + API)."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from reelbox import db
from reelbox.config import Settings

from .conftest import analyzed_item
from .test_organize import default_llm, primary_topics, run
from .test_web import AUTH, client, library, web_settings  # noqa: F401 (픽스처 재사용)


def _topic(conn: sqlite3.Connection, name: str) -> db.Topic:
    return next(t for t in db.list_topics(conn) if t.name == name)


# --- 주제 이름 바꾸기 ---


def test_rename_topic_fixes_it(client: TestClient, library: dict[str, int]) -> None:  # noqa: F811
    res = client.patch(f"/api/topics/{library['cook']}", json={"name": "  집밥 "}, headers=AUTH)
    assert res.status_code == 200
    assert (res.json()["name"], res.json()["count"]) == ("집밥", 2)
    topics = {t["name"]: t for t in client.get("/api/topics", headers=AUTH).json()}
    assert topics["집밥"]["fixed"] is True
    assert topics["운동"]["fixed"] is False


def test_rename_rejects_taken_blank_and_unknown(
    client: TestClient,  # noqa: F811
    library: dict[str, int],  # noqa: F811
) -> None:
    taken = client.patch(f"/api/topics/{library['cook']}", json={"name": "운동"}, headers=AUTH)
    assert taken.status_code == 409 and "이미 있어요" in taken.json()["detail"]
    assert (
        client.patch(f"/api/topics/{library['cook']}", json={"name": "  "}, headers=AUTH)
    ).status_code == 422
    assert client.patch("/api/topics/999", json={"name": "x"}, headers=AUTH).status_code == 404
    # 같은 이름으로 다시 저장은 괜찮다
    same = client.patch(f"/api/topics/{library['cook']}", json={"name": "요리"}, headers=AUTH)
    assert same.status_code == 200


# --- 주제 합치기 ---


def test_merge_moves_items_and_deletes_source(
    client: TestClient,  # noqa: F811
    library: dict[str, int],  # noqa: F811
    conn: sqlite3.Connection,
) -> None:
    res = client.post(
        f"/api/topics/{library['gym']}/merge", json={"into": library["cook"]}, headers=AUTH
    )
    assert res.status_code == 200
    assert (res.json()["id"], res.json()["count"]) == (library["cook"], 3)
    assert [t.name for t in db.list_topics(conn)] == ["요리"]
    assert _topic(conn, "요리").is_user_edited
    stretch = client.get(f"/api/items/{library['stretch']}", headers=AUTH).json()
    assert stretch["topic"] == "요리"


def test_merge_rejects_same_and_unknown(
    client: TestClient,  # noqa: F811
    library: dict[str, int],  # noqa: F811
) -> None:
    cook = library["cook"]
    assert (
        client.post(f"/api/topics/{cook}/merge", json={"into": cook}, headers=AUTH)
    ).status_code == 400
    assert (
        client.post(f"/api/topics/{cook}/merge", json={"into": 999}, headers=AUTH)
    ).status_code == 404


# --- 영상 주제 바꾸기 ---


def test_move_item_to_topic(
    client: TestClient,  # noqa: F811
    library: dict[str, int],  # noqa: F811
    conn: sqlite3.Connection,
) -> None:
    item, gym = library["pasta"], library["gym"]
    res = client.put(f"/api/items/{item}/topic", json={"topic_id": gym}, headers=AUTH)
    assert res.status_code == 200
    detail = client.get(f"/api/items/{item}", headers=AUTH).json()
    assert (detail["topic_id"], detail["topic"]) == (gym, "운동")
    assert _topic(conn, "운동").is_user_edited  # 옮겨 간 주제는 고정
    row = conn.execute("SELECT is_user_set FROM item_topics WHERE item_id = ?", (item,)).fetchone()
    assert row["is_user_set"] == 1
    counts = {t["name"]: t["count"] for t in client.get("/api/topics", headers=AUTH).json()}
    assert counts == {"요리": 1, "운동": 2}
    assert (
        client.put(f"/api/items/{item}/topic", json={"topic_id": 999}, headers=AUTH)
    ).status_code == 404
    assert (
        client.put("/api/items/999/topic", json={"topic_id": gym}, headers=AUTH)
    ).status_code == 404


def test_full_organize_keeps_items_i_moved(conn: sqlite3.Connection, settings: Settings) -> None:
    a = analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피")
    b = analyzed_item(conn, "bbbbbbbbbbb", raw_topic="파스타 레시피")
    c = analyzed_item(conn, "ccccccccccc", raw_topic="아이메이크업")
    run(conn, settings, default_llm())
    assert primary_topics(conn) == {a: "요리", b: "요리", c: "뷰티"}

    assert db.set_item_topic(conn, a, _topic(conn, "뷰티").id)
    run(conn, settings, default_llm(), full=True)

    # 내가 옮긴 a 는 뷰티에 남고, 나머지는 대응표대로 다시 배정된다
    assert primary_topics(conn) == {a: "뷰티", b: "요리", c: "뷰티"}
    assert _topic(conn, "뷰티").is_user_edited


def test_full_organize_keeps_merged_topic(conn: sqlite3.Connection, settings: Settings) -> None:
    a = analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피")
    c = analyzed_item(conn, "ccccccccccc", raw_topic="아이메이크업")
    run(conn, settings, default_llm())
    db.merge_topics(conn, _topic(conn, "뷰티").id, _topic(conn, "요리").id)
    assert primary_topics(conn) == {a: "요리", c: "요리"}

    run(conn, settings, default_llm(), full=True)

    # 남긴 주제(요리)는 고정이라 지워지지 않는다. 뷰티는 자동 정리가 다시 만들 수 있다.
    assert _topic(conn, "요리").is_user_edited
    assert primary_topics(conn)[a] == "요리"


# --- 즐겨찾기·메모 ---


def test_favorite_and_note(client: TestClient, library: dict[str, int]) -> None:  # noqa: F811
    item = library["tomato"]
    url = f"/api/items/{item}"
    assert client.patch(url, json={"favorite": True}, headers=AUTH).json() == {"ids": [item]}
    assert client.patch(url, json={"note": "  주말에 해 보기 "}, headers=AUTH).status_code == 200
    body = client.get(url, headers=AUTH).json()
    assert (body["favorite"], body["note"], body["hidden"]) == (True, "주말에 해 보기", False)

    # 메모만 지워도 즐겨찾기는 그대로
    client.patch(url, json={"note": ""}, headers=AUTH)
    body = client.get(url, headers=AUTH).json()
    assert (body["favorite"], body["note"]) == (True, None)
    client.patch(url, json={"favorite": False, "note": None}, headers=AUTH)
    assert client.get(url, headers=AUTH).json()["favorite"] is False


def test_patch_validation(client: TestClient, library: dict[str, int]) -> None:  # noqa: F811
    url = f"/api/items/{library['tomato']}"
    assert client.patch(url, json={}, headers=AUTH).status_code == 422
    assert client.patch(url, json={"favorite": None}, headers=AUTH).status_code == 422
    too_long = "가" * (db.NOTE_MAX + 1)
    assert client.patch(url, json={"note": too_long}, headers=AUTH).status_code == 422
    assert client.patch("/api/items/999", json={"favorite": True}, headers=AUTH).status_code == 404


def test_hidden_patch_still_works(client: TestClient, library: dict[str, int]) -> None:  # noqa: F811
    url = f"/api/items/{library['tomato']}"
    assert client.patch(url, json={"hidden": True}, headers=AUTH).status_code == 200
    assert client.get(url, headers=AUTH).json()["hidden"] is True


def test_search_favorite_filter(client: TestClient, library: dict[str, int]) -> None:  # noqa: F811
    client.patch(f"/api/items/{library['tomato']}", json={"favorite": True}, headers=AUTH)
    everything = client.get("/api/search?q=파스타", headers=AUTH).json()
    assert {c["id"] for c in everything["items"]} >= {library["pasta"], library["tomato"]}
    fav = client.get("/api/search?q=파스타&favorite=true", headers=AUTH).json()
    assert [c["id"] for c in fav["items"]] == [library["tomato"]]
    assert fav["items"][0]["favorite"] is True


@pytest.mark.parametrize("value", [True, False])
def test_set_favorite_db(conn: sqlite3.Connection, value: bool) -> None:
    item = analyzed_item(conn, "aaaaaaaaaaa")
    assert db.set_favorite(conn, [item, 999], value) == [item]
    assert db.cards_by_ids(conn, [item])[0].favorite is value
