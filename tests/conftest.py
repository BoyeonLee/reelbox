"""공용 픽스처. 실제 data/ 와 .env 는 건드리지 않고 tmp_path 만 쓴다."""

import sqlite3
from collections.abc import Iterator

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
