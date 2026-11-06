"""색인: analyzed 항목을 키워드 색인(FTS5)과 벡터 색인(sqlite-vec)에 넣고 indexed 로 넘긴다.

끝나면 색인된 모든 항목의 "비슷한 영상"을 다시 계산해 item_similar 에 저장한다.
"""

import logging
import sqlite3
from collections.abc import Callable

import numpy as np
from pydantic import BaseModel

from reelbox import db
from reelbox.config import Settings
from reelbox.search.embed import Embedder, Matrix, encode_batched, item_text

logger = logging.getLogger(__name__)

ProgressFn = Callable[[int, int], None]


class IndexReport(BaseModel):
    indexed: int = 0
    embedded: int = 0  # 이번에 새로 임베딩을 계산한 수
    reused: int = 0  # organize 가 미리 계산해 둔 임베딩을 그대로 쓴 수
    similar_items: int = 0  # 비슷한 영상을 계산한 항목 수


def ensure_vectors(
    conn: sqlite3.Connection,
    settings: Settings,
    embedder: Embedder,
    items: list[db.EmbedInput],
    on_progress: ProgressFn | None = None,
) -> tuple[dict[int, list[float]], int]:
    """items 의 임베딩. 저장된 게 있으면 쓰고, 없으면 계산해서 저장한다.

    돌려주는 값: (항목 id → 벡터, 새로 계산한 개수).
    organize 가 index 보다 먼저 돌기 때문에, 먼저 계산한 쪽이 저장해 두고 다른 쪽이 다시 쓴다.
    (분석을 다시 하는 명령이 생기면 그때 이 임베딩도 지워야 한다.)
    """
    vectors = db.load_vectors(conn, [it.id for it in items])
    missing = [it for it in items if it.id not in vectors]
    if missing:
        texts = [item_text(it, settings.index_transcript_chars) for it in missing]
        matrix = encode_batched(embedder, texts, settings.embed_batch_size, on_progress)
        new = {it.id: matrix[n].tolist() for n, it in enumerate(missing)}
        db.save_vectors(conn, new)
        vectors.update(new)
    return vectors, len(missing)


def top_similar(ids: list[int], matrix: Matrix, k: int) -> list[tuple[int, int, int, float]]:
    """각 항목과 코사인 유사도가 가장 높은 k 개. 결과는 (item_id, similar_id, rank, score).

    벡터가 정규화돼 있어서 matrix @ matrix.T 한 번으로 모든 쌍의 유사도가 나온다
    (항목 1,000개면 100만 쌍이지만 numpy 로 1초도 안 걸린다).
    """
    if len(ids) < 2 or k <= 0:
        return []
    scores = matrix @ matrix.T
    np.fill_diagonal(scores, -np.inf)  # 자기 자신은 빼기
    k = min(k, len(ids) - 1)
    rows: list[tuple[int, int, int, float]] = []
    for i, item_id in enumerate(ids):
        best = np.argsort(-scores[i])[:k]
        for rank, j in enumerate(best, start=1):
            rows.append((item_id, ids[j], rank, float(scores[i, j])))
    return rows


def refresh_similar(conn: sqlite3.Connection, k: int) -> int:
    """보여줄 수 있는 색인 항목 전체의 비슷한 영상을 다시 계산한다. 계산한 항목 수를 돌려준다."""
    vectors = db.indexed_vectors(conn)
    ids = list(vectors)
    matrix = np.array([vectors[i] for i in ids], dtype=np.float32).reshape(len(ids), -1)
    db.replace_similar(conn, top_similar(ids, matrix, k))
    return len(ids) if len(ids) >= 2 else 0


def index_items(
    conn: sqlite3.Connection,
    settings: Settings,
    embedder: Embedder,
    *,
    limit: int | None = None,
    on_progress: ProgressFn | None = None,
) -> IndexReport:
    """analyzed 항목을 색인한다. indexed 는 다시 건드리지 않는다(멱등)."""
    items = db.index_candidates(conn, limit=limit)
    report = IndexReport()
    if items:
        _, embedded = ensure_vectors(conn, settings, embedder, items, on_progress)
        report.embedded = embedded
        report.reused = len(items) - embedded
        for item in items:
            db.save_index(conn, item)
        report.indexed = len(items)
    # 숨김·삭제가 바뀌었을 수도 있으니, 새로 색인한 게 없어도 비슷한 영상은 다시 계산한다
    report.similar_items = refresh_similar(conn, settings.similar_count)
    return report
