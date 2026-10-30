"""SQLite 연결, 번호별 마이그레이션, 저장소 함수."""

import logging
import re
import sqlite3
from pathlib import Path

import sqlite_vec
from pydantic import BaseModel

from reelbox.config import Settings

logger = logging.getLogger(__name__)

# DESIGN.md 4장 "status 흐름" 순서
ITEM_STATUSES: tuple[str, ...] = (
    "imported",
    "enriched",
    "analyzed",
    "indexed",
    "failed",
    "unavailable",
    "skipped",
    "deleted",
)

_MIGRATION_RE = re.compile(r"^(\d{3})_[\w-]+\.sql$")


class InitResult(BaseModel):
    db_path: Path
    applied: list[str]
    version: int


class FailedItem(BaseModel):
    id: int
    platform: str
    url: str
    error: str | None
    attempts: int
    updated_at: str


def connect(db_path: Path) -> sqlite3.Connection:
    """sqlite-vec 을 불러온 연결을 연다. 확장을 못 불러오면 예외를 그대로 올린다."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _migration_files(migrations_dir: Path) -> list[tuple[int, Path]]:
    files: list[tuple[int, Path]] = []
    for path in migrations_dir.glob("*.sql"):
        m = _MIGRATION_RE.match(path.name)
        if m is None:
            raise ValueError(f"마이그레이션 파일 이름 형식이 틀림 (NNN_name.sql): {path.name}")
        files.append((int(m.group(1)), path))
    files.sort()
    numbers = [n for n, _ in files]
    if len(numbers) != len(set(numbers)):
        raise ValueError(f"마이그레이션 번호가 중복됨: {numbers}")
    return files


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, migrations_dir: Path) -> list[str]:
    """아직 적용 안 된 마이그레이션을 번호순으로 적용하고, 적용한 파일 이름을 돌려준다.

    적용한 마지막 번호는 PRAGMA user_version 에 기록한다. 파일 하나는 트랜잭션 하나라서
    중간에 실패하면 그 파일 전체가 되돌려진다.
    """
    current = schema_version(conn)
    applied: list[str] = []
    for number, path in _migration_files(migrations_dir):
        if number <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        logger.info("마이그레이션 적용: %s", path.name)
        applied.append(path.name)
    return applied


def init_db(settings: Settings) -> InitResult:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        applied = migrate(conn, settings.migrations_dir)
        return InitResult(db_path=settings.db_path, applied=applied, version=schema_version(conn))
    finally:
        conn.close()


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """상태별 항목 수. 항목이 없는 상태도 0으로 포함한다."""
    counts = dict.fromkeys(ITEM_STATUSES, 0)
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status"):
        counts[row["status"]] = row["n"]
    return counts


def recent_failures(conn: sqlite3.Connection, limit: int = 5) -> list[FailedItem]:
    rows = conn.execute(
        """
        SELECT id, platform, url, error, attempts, updated_at
        FROM items
        WHERE status = 'failed'
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [FailedItem(**dict(row)) for row in rows]
