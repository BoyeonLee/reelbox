"""줄마다 링크 하나인 URL 목록 파일 가져오기.

빈 줄과 '#' 으로 시작하는 줄은 건너뛴다. 줄에 다른 글자가 섞여 있으면 첫 http(s) 링크를 쓴다.
"""

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from reelbox import db
from reelbox.importers import ImportReport

logger = logging.getLogger(__name__)

SOURCE = "urllist"
_URL_IN_TEXT = re.compile(r"https?://\S+")


def import_urls(
    conn: sqlite3.Connection,
    path: Path,
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> ImportReport:
    """저장 시각을 모르므로 가져온 시각(처음 발견한 시각)을 saved_at 에 넣는다."""
    saved_at = now or datetime.now(UTC)
    report = ImportReport()
    processed = 0
    # utf-8-sig: 윈도우 메모장이 붙이는 BOM 을 떼고 읽는다
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    with conn:
        for lineno, line in enumerate(lines, start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if limit is not None and processed >= limit:
                break
            processed += 1
            m = _URL_IN_TEXT.search(text)
            url = m.group(0) if m else text
            outcome = db.add_item(conn, url, saved_at=saved_at, source=SOURCE)
            if outcome is db.AddOutcome.UNRECOGNIZED:
                logger.warning("%s:%d 인식 못 한 링크: %s", path.name, lineno, url)
            report.record(outcome)
    return report
