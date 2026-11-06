"""공용 픽스처. 실제 data/ 와 .env 는 건드리지 않고 tmp_path 만 쓴다."""

import json
import sqlite3
import zlib
from collections.abc import Iterator

import numpy as np
import pytest

from reelbox import db
from reelbox.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path / "data")


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    db.init_db(settings)
    c = db.connect(settings.db_path)
    yield c
    c.close()


def insert_item(conn: sqlite3.Connection, source_id: str, **cols: object) -> int:
    """테스트용 items 한 줄 넣기."""
    values = {
        "platform": "youtube",
        "source_id": source_id,
        "url": f"https://www.youtube.com/shorts/{source_id}",
        **cols,
    }
    keys = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO items ({keys}) VALUES ({marks})", tuple(values.values()))
    conn.commit()
    return cur.lastrowid


class FakeEmbedder:
    """모델 없이 쓰는 가짜 임베딩. 글자 2개 조각(bigram)을 해시해 1024칸에 세고 정규화한다.

    글자가 많이 겹치는 글끼리 가까워서, 진짜 모델은 아니어도 순위 테스트에 쓸 만하다.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        out = np.zeros((len(texts), 1024), dtype=np.float32)
        for row, text in enumerate(texts):
            compact = "".join(text.split())
            for i in range(len(compact) - 1):
                out[row, zlib.crc32(compact[i : i + 2].encode()) % 1024] += 1.0
            norm = np.linalg.norm(out[row])
            if norm > 0:
                out[row] /= norm
        return out

    @property
    def encoded(self) -> int:
        return sum(len(c) for c in self.calls)


def analyzed_item(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    title: str = "",
    summary: str = "",
    raw_topic: str = "기타",
    tags: list[str] | None = None,
    transcript: str | None = None,
    status: str = "analyzed",
    **cols: object,
) -> int:
    """분석까지 끝난 항목 하나 (items + transcripts + analyses)."""
    item_id = insert_item(conn, source_id, title=title, status=status, **cols)
    conn.execute(
        "INSERT INTO transcripts (item_id, source, language, text) VALUES (?, ?, 'ko', ?)",
        (item_id, "captions" if transcript else "none", transcript),
    )
    conn.execute(
        """
        INSERT INTO analyses (item_id, summary_ko, key_points, tags, raw_topic,
                              content_language, model, prompt_version)
        VALUES (?, ?, '[]', ?, ?, 'ko', 'fake-model', 'analyze_v1')
        """,
        (item_id, summary, json.dumps(tags or [], ensure_ascii=False), raw_topic),
    )
    conn.commit()
    return item_id


def set_user_meta(conn: sqlite3.Connection, item_id: int, **cols: object) -> None:
    keys = ", ".join(["item_id", *cols])
    marks = ", ".join("?" for _ in range(len(cols) + 1))
    conn.execute(f"INSERT INTO user_meta ({keys}) VALUES ({marks})", (item_id, *cols.values()))
    conn.commit()
