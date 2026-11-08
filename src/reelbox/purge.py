"""휴지통 영구 정리 (DESIGN.md 4장 "숨기기와 삭제").

휴지통에 넣은 지 30일이 지난 항목의 자막·분석·임베딩·색인·주제 연결과 썸네일 파일을 지우고,
items 행만 status = 'deleted' 로 남긴다. `reelbox purge` 와 서버의 하루 한 번 자동 실행,
화면의 "휴지통 비우기"가 모두 이 함수를 쓴다.
"""

import logging
import sqlite3
from datetime import datetime, timedelta

from reelbox import db
from reelbox.config import Settings

logger = logging.getLogger(__name__)

TRASH_DAYS = 30


def purge(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime,
    days: int | None = TRASH_DAYS,
) -> list[int]:
    """days 일보다 오래 휴지통에 있던 항목을 정리한다. days 가 None 이면 휴지통 전부.

    정리한 항목 id 를 돌려준다. 여러 번 돌려도 이미 정리한 것은 다시 건드리지 않는다(멱등).
    """
    before = None if days is None else now - timedelta(days=days)
    purged = db.purge_trash(conn, deleted_before=before)
    thumbs_root = settings.data_dir.resolve()
    for item in purged:
        if not item.thumbnail_path:
            continue
        path = (settings.data_dir / item.thumbnail_path).resolve()
        # data_dir 밖의 파일은 절대 지우지 않는다
        if not path.is_relative_to(thumbs_root):
            logger.warning("data 폴더 밖 썸네일은 지우지 않음: %s", path)
            continue
        path.unlink(missing_ok=True)
    if purged:
        logger.info("휴지통 정리: %d개", len(purged))
    return [p.id for p in purged]
