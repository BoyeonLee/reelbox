"""하이브리드 검색: 키워드 검색과 의미(벡터) 검색 결과를 RRF 로 합친다.

## RRF (Reciprocal Rank Fusion, 순위 역수 합치기)

도서관 사서 여러 명에게 같은 질문을 하고 각자 추천 순위표를 받았다고 하자.
- 사서 A(키워드): "김치찌개"라는 글자가 들어간 책을 정확히 찾는다.
- 사서 B(의미): 글자는 달라도 "찌개 끓이는 법"처럼 뜻이 비슷한 책을 찾는다.

두 사람의 점수는 단위가 달라서(BM25 점수 12.3 과 코사인 유사도 0.71) 그냥 더할 수 없다.
RRF 는 점수 대신 **순위**만 쓴다. 각 순위표에서 책이 r 등이면 1 / (k + r) 점을 주고 더한다.

    RRF 점수 = Σ (순위표마다) 1 / (k + 그 순위표에서의 순위)

k 는 보통 60 을 쓴다 (원 논문 값). k 가 있으면 1등과 2등의 점수 차가 너무 크지 않아서,
한 사서만 1등으로 꼽은 책보다 **두 사서가 모두 상위권으로 꼽은 책**이 위로 올라온다.
예 (k = 60):
- 키워드 1등, 의미 순위표에 없음 → 1/61                 ≈ 0.0164
- 키워드 3등, 의미 2등              → 1/63 + 1/62         ≈ 0.0320  ← 더 위
순위표에 없는 책은 그 순위표에서 0점이다. 점수를 맞추는 작업이 필요 없어서 단순하고 튼튼하다.

## 순위표 세 개
1. keyword: FTS5 BM25. trigram 색인이라 3글자 이상인 단어만 찾을 수 있다.
2. short:   2글자 이하 단어("김밥", "간식")는 trigram 색인으로 못 찾아서 LIKE 로 찾는다.
            항목이 수천 개 이하라 전체를 훑어도 빠르다.
3. vector:  bge-m3 벡터의 코사인 거리. 필터를 먼저 걸고 거리를 전부 계산한다
            (KNN 으로 먼저 뽑고 필터를 나중에 걸면 결과가 비어 버릴 수 있어서).
"""

import logging
import sqlite3
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel

from reelbox import db
from reelbox.search.embed import Embedder

logger = logging.getLogger(__name__)

RRF_K = 60
LIST_SIZE = 50  # 순위표마다 상위 몇 개까지 합칠지

ListName = Literal["keyword", "short", "vector"]

# 열 순서는 items_fts 정의와 같다: title, description, transcript, summary, tags.
# 제목·태그에 걸리면 자막에 걸린 것보다 훨씬 중요하다.
COLUMN_WEIGHTS: dict[str, float] = {
    "title": 3.0,
    "description": 1.0,
    "transcript": 0.5,
    "summary": 2.0,
    "tags": 3.0,
}
MIN_TRIGRAM_CHARS = 3


class SearchFilters(BaseModel):
    platform: str | None = None
    topic: str | None = None  # 주제 이름 또는 id
    since: date | None = None  # 저장한 날짜 (이날 포함)
    until: date | None = None  # 저장한 날짜 (이날 포함)
    favorite: bool = False


class SearchHit(BaseModel):
    id: int
    platform: str
    url: str
    title: str | None
    author: str | None
    summary_ko: str | None
    topic: str | None  # 대표 주제 이름
    saved_at: str | None
    score: float  # RRF 점수
    ranks: dict[str, int]  # 순위표 이름 → 그 안에서의 순위 (어디서 걸렸는지 확인용)


def rrf_merge(lists: dict[str, list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """순위표 여러 개를 RRF 로 합친다. 점수 높은 순 (같으면 id 작은 순)."""
    scores: dict[int, float] = {}
    for ranked in lists.values():
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def split_terms(query: str) -> tuple[list[str], list[str]]:
    """검색어를 (3글자 이상, 2글자 이하) 단어로 나눈다. 중복은 뺀다."""
    terms = list(dict.fromkeys(query.split()))
    long = [t for t in terms if len(t) >= MIN_TRIGRAM_CHARS]
    short = [t for t in terms if len(t) < MIN_TRIGRAM_CHARS]
    return long, short


def _filter_sql(filters: SearchFilters) -> tuple[str, dict[str, object]]:
    """items 별칭 i 에 붙일 WHERE 조건과 파라미터. 색인된·보여줄 수 있는 항목만."""
    clauses = ["i.status = 'indexed'", db.VISIBLE_SQL]
    params: dict[str, object] = {}
    if filters.platform:
        clauses.append("i.platform = :platform")
        params["platform"] = filters.platform
    if filters.topic:
        clauses.append(
            """EXISTS (
                SELECT 1 FROM item_topics AS it JOIN topics AS tp ON tp.id = it.topic_id
                WHERE it.item_id = i.id
                  AND (tp.name = :topic OR CAST(tp.id AS TEXT) = :topic))"""
        )
        params["topic"] = filters.topic
    if filters.since:
        clauses.append("i.saved_at >= :since")
        params["since"] = filters.since.isoformat()
    if filters.until:
        # saved_at 은 '2026-10-30T12:00:00Z' 형식이라 다음 날 0시 미만으로 비교한다
        clauses.append("i.saved_at < :until")
        params["until"] = (filters.until + timedelta(days=1)).isoformat()
    if filters.favorite:
        clauses.append(
            "EXISTS (SELECT 1 FROM user_meta AS fm WHERE fm.item_id = i.id AND fm.favorite = 1)"
        )
    return " AND ".join(clauses), params


def keyword_ranking(
    conn: sqlite3.Connection, terms: list[str], filters: SearchFilters
) -> list[int]:
    """FTS5 BM25 순위. 단어 중 하나라도 들어 있으면 후보 (많이 들어 있을수록 위)."""
    if not terms:
        return []
    # 단어를 큰따옴표로 감싸야 FTS5 문법 문자(-, *, : 등)가 검색어로 취급된다
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    where, params = _filter_sql(filters)
    weights = ", ".join(str(w) for w in COLUMN_WEIGHTS.values())
    rows = conn.execute(
        f"""
        SELECT i.id
        FROM items_fts AS f JOIN items AS i ON i.id = f.rowid
        WHERE items_fts MATCH :match AND {where}
        ORDER BY bm25(items_fts, {weights}), i.id
        LIMIT {LIST_SIZE}
        """,
        {**params, "match": match},
    )
    return [r["id"] for r in rows]


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def short_term_ranking(
    conn: sqlite3.Connection, terms: list[str], filters: SearchFilters
) -> list[int]:
    """2글자 이하 단어를 LIKE 로 찾는다. 점수 = 단어가 들어 있는 열의 가중치 합."""
    if not terms:
        return []
    where, params = _filter_sql(filters)
    parts: list[str] = []
    for n, term in enumerate(terms):
        params[f"p{n}"] = _like_pattern(term)
        for column, weight in COLUMN_WEIGHTS.items():
            parts.append(f"(f.{column} LIKE :p{n} ESCAPE '\\') * {weight}")
    score = " + ".join(parts)
    rows = conn.execute(
        f"""
        SELECT i.id, {score} AS score
        FROM items_fts AS f JOIN items AS i ON i.id = f.rowid
        WHERE {where} AND score > 0
        ORDER BY score DESC, i.id
        LIMIT {LIST_SIZE}
        """,
        params,
    )
    return [r["id"] for r in rows]


def vector_ranking(
    conn: sqlite3.Connection, query_vector: list[float], filters: SearchFilters
) -> list[int]:
    """코사인 거리가 가까운 순. 필터를 먼저 건 뒤 전부 계산한다."""
    where, params = _filter_sql(filters)
    rows = conn.execute(
        f"""
        SELECT i.id, vec_distance_cosine(e.vector, :q) AS d
        FROM embeddings AS e JOIN items AS i ON i.id = e.item_id
        WHERE {where}
        ORDER BY d, i.id
        LIMIT {LIST_SIZE}
        """,
        {**params, "q": db.vector_to_blob(query_vector)},
    )
    return [r["id"] for r in rows]


def _load_hits(
    conn: sqlite3.Connection,
    merged: list[tuple[int, float]],
    lists: dict[str, list[int]],
) -> list[SearchHit]:
    if not merged:
        return []
    ids = [item_id for item_id, _ in merged]
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT i.id, i.platform, i.url, i.title, i.author, i.saved_at, a.summary_ko,
               (SELECT tp.name FROM item_topics AS it JOIN topics AS tp ON tp.id = it.topic_id
                WHERE it.item_id = i.id AND it.is_primary = 1) AS topic
        FROM items AS i LEFT JOIN analyses AS a ON a.item_id = i.id
        WHERE i.id IN ({marks})
        """,
        ids,
    )
    by_id = {r["id"]: dict(r) for r in rows}
    positions = {
        name: {item_id: n for n, item_id in enumerate(ranked, start=1)}
        for name, ranked in lists.items()
    }
    hits: list[SearchHit] = []
    for item_id, score in merged:
        ranks = {name: pos[item_id] for name, pos in positions.items() if item_id in pos}
        hits.append(SearchHit(**by_id[item_id], score=score, ranks=ranks))
    return hits


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    embedder: Embedder | None,
    filters: SearchFilters | None = None,
    limit: int = 10,
) -> list[SearchHit]:
    """검색어로 찾은 항목을 RRF 점수 순으로. embedder 가 None 이면 키워드 검색만 한다."""
    filters = filters or SearchFilters()
    query = query.strip()
    if not query:
        return []
    long, short = split_terms(query)
    lists: dict[str, list[int]] = {
        "keyword": keyword_ranking(conn, long, filters),
        "short": short_term_ranking(conn, short, filters),
    }
    if embedder is not None:
        query_vector = embedder.encode([query])[0].tolist()
        lists["vector"] = vector_ranking(conn, query_vector, filters)
    lists = {name: ranked for name, ranked in lists.items() if ranked}
    merged = rrf_merge(lists)[:limit]
    return _load_hits(conn, merged, lists)
