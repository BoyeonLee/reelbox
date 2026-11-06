"""하이브리드 검색: RRF, 3글자 이상(FTS5)·2글자 이하(LIKE)·벡터 순위표, 필터."""

import sqlite3
from datetime import date

import pytest

from reelbox import db
from reelbox.config import Settings
from reelbox.search import index as indexer
from reelbox.search import query as searcher
from reelbox.search.query import SearchFilters

from .conftest import FakeEmbedder, analyzed_item, set_user_meta


def test_rrf_merge_matches_docstring_example() -> None:
    # A 는 키워드 1등만, B 는 키워드 3등 + 의미 2등 → B 가 위
    merged = searcher.rrf_merge({"keyword": [1, 9, 2], "vector": [8, 2]})
    scores = dict(merged)
    assert merged[0][0] == 2
    assert scores[2] == pytest.approx(1 / 63 + 1 / 62)
    assert scores[1] == pytest.approx(1 / 61)


def test_rrf_merge_ties_by_id() -> None:
    assert [i for i, _ in searcher.rrf_merge({"a": [5], "b": [3]})] == [3, 5]


def test_split_terms() -> None:
    assert searcher.split_terms("김밥 파스타 김밥  a 브라운버터") == (
        ["파스타", "브라운버터"],
        ["김밥", "a"],
    )


@pytest.fixture
def library(conn: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    ids = {
        "kimbap": analyzed_item(
            conn,
            "aaaaaaaaaaa",
            title="서울 김밥 맛집 3곳",
            summary="을지로 김밥집 소개",
            tags=["김밥", "맛집"],
            saved_at="2026-01-10T09:00:00Z",
        ),
        "pasta": analyzed_item(
            conn,
            "bbbbbbbbbbb",
            title="브라운버터 파스타",
            summary="버터를 갈색이 될 때까지 녹여 만드는 파스타",
            tags=["파스타", "브라운버터"],
            saved_at="2026-03-05T09:00:00Z",
        ),
        "beauty": analyzed_item(
            conn,
            "ccccccccccc",
            platform="instagram",
            title="다이소 브러시 추천",
            summary="다이소 뷰티 브러시 비교",
            tags=["다이소", "브러시"],
            transcript="오늘은 파스타 말고 브러시 얘기",
            saved_at="2026-06-01T09:00:00Z",
        ),
    }
    indexer.index_items(conn, settings, FakeEmbedder())
    return ids


def _ids(hits: list[searcher.SearchHit]) -> list[int]:
    return [h.id for h in hits]


def test_keyword_search_ranks_title_and_tags_above_transcript(
    conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    hits = searcher.search(conn, "파스타", embedder=None)
    assert _ids(hits) == [library["pasta"], library["beauty"]]
    assert hits[0].ranks == {"keyword": 1}
    assert hits[0].summary_ko and hits[0].url.endswith("bbbbbbbbbbb")


def test_two_letter_term_is_found_with_like(
    conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    # trigram 색인만으로는 2글자 '김밥' 을 못 찾는다
    assert searcher.keyword_ranking(conn, ["김밥"], SearchFilters()) == []
    hits = searcher.search(conn, "김밥", embedder=None)
    assert _ids(hits) == [library["kimbap"]]
    assert hits[0].ranks == {"short": 1}


def test_mixed_terms_combine_lists(conn: sqlite3.Connection, library: dict[str, int]) -> None:
    hits = searcher.search(conn, "김밥 브러시", embedder=None)
    assert set(_ids(hits)) == {library["kimbap"], library["beauty"]}


def test_vector_list_is_added_when_embedder_given(
    conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    hits = searcher.search(conn, "버터 녹이기", embedder=FakeEmbedder(), limit=3)
    assert hits[0].id == library["pasta"]
    assert "vector" in hits[0].ranks
    assert len(hits) == 3  # 의미 순위표는 키워드가 안 걸려도 결과를 낸다


def test_fts_syntax_characters_do_not_break_search(
    conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    for q in ['"파스타', "파스타*", "a-b OR c:d", "AND"]:
        searcher.search(conn, q, embedder=None)  # 예외 없이


def test_empty_query(conn: sqlite3.Connection, library: dict[str, int]) -> None:
    assert searcher.search(conn, "   ", embedder=FakeEmbedder()) == []


def test_filter_platform(conn: sqlite3.Connection, library: dict[str, int]) -> None:
    hits = searcher.search(
        conn, "파스타", embedder=FakeEmbedder(), filters=SearchFilters(platform="instagram")
    )
    assert _ids(hits) == [library["beauty"]]


def test_filter_saved_period_includes_both_ends(
    conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    f = SearchFilters(since=date(2026, 1, 10), until=date(2026, 3, 5))
    hits = searcher.search(conn, "파스타 김밥 브러시", embedder=FakeEmbedder(), filters=f)
    assert set(_ids(hits)) == {library["kimbap"], library["pasta"]}


def test_filter_topic_by_name_or_id(conn: sqlite3.Connection, library: dict[str, int]) -> None:
    cur = conn.execute("INSERT INTO topics (name) VALUES ('요리')")
    topic_id = cur.lastrowid
    conn.commit()
    db.assign_topics(conn, {library["pasta"]: topic_id, library["kimbap"]: topic_id})
    for value in ("요리", str(topic_id)):
        hits = searcher.search(
            conn, "파스타 브러시", embedder=FakeEmbedder(), filters=SearchFilters(topic=value)
        )
        assert set(_ids(hits)) == {library["pasta"], library["kimbap"]}
    hit = searcher.search(conn, "파스타", embedder=None)[0]
    assert hit.topic == "요리"


def test_filter_favorite(conn: sqlite3.Connection, library: dict[str, int]) -> None:
    set_user_meta(conn, library["beauty"], favorite=1)
    hits = searcher.search(
        conn, "파스타", embedder=FakeEmbedder(), filters=SearchFilters(favorite=True)
    )
    assert _ids(hits) == [library["beauty"]]


def test_hidden_and_deleted_are_never_returned(
    conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    set_user_meta(conn, library["pasta"], hidden_at="2026-11-01T00:00:00Z")
    set_user_meta(conn, library["beauty"], deleted_at="2026-11-01T00:00:00Z")
    hits = searcher.search(conn, "파스타 김밥", embedder=FakeEmbedder())
    assert _ids(hits) == [library["kimbap"]]


def test_unindexed_items_are_not_returned(conn: sqlite3.Connection, settings: Settings) -> None:
    analyzed_item(conn, "aaaaaaaaaaa", title="파스타")  # analyzed, 아직 색인 안 함
    assert searcher.search(conn, "파스타", embedder=None) == []
