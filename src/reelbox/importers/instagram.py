"""인스타그램 '내 정보 다운로드' 파일 파서.

형식이 버전마다 달라서(옛 형식: saved_saved_media/string_map_data,
2026년 형식: label_values) 키 이름에 기대지 않고, 항목마다 href 안의 게시물 링크와
가장 가까운 상위의 timestamp 를 재귀로 찾는다.
"""

import json
import logging
import sqlite3
import zipfile
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reelbox import db
from reelbox.importers import ImportReport
from reelbox.urls import parse_url

logger = logging.getLogger(__name__)

SOURCE = "instagram_export"
POSTS_FILE = "saved_posts.json"
COLLECTIONS_FILE = "saved_collections.json"
# 컬렉션 이름이 들어 있는 라벨 (계정 언어에 따라 다름)
NAME_LABELS = {"이름", "Name"}
# 2026년 형식의 캡션·작성자 라벨. 없으면 비워 두고 보강 단계가 채운다.
CAPTION_LABELS = {"캡션", "Caption"}
OWNER_TITLES = {"소유자", "Owner"}
USERNAME_LABELS = {"사용자 이름", "Username"}


def fix_mojibake(text: str) -> str:
    """메타 내보내기는 UTF-8 바이트를 글자 하나씩 latin-1 로 저장한다 ('캡션' → 'ìº¡ì\\x85\\x98').

    되돌릴 수 있으면 되돌리고, 아니면(이미 정상인 글자) 그대로 둔다.
    """
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _fix_tree(node: Any) -> Any:
    if isinstance(node, str):
        return fix_mojibake(node)
    if isinstance(node, list):
        return [_fix_tree(x) for x in node]
    if isinstance(node, dict):
        return {fix_mojibake(k): _fix_tree(v) for k, v in node.items()}
    return node


def _load_json(path: Path, filename: str) -> Any | None:
    """폴더(하위 전체) 또는 zip 안에서 파일 이름으로 찾아 읽는다. 없으면 None."""
    if path.is_dir():
        matches = sorted(path.rglob(filename))
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning("%s 가 여러 개라 첫 번째를 씀: %s", filename, matches[0])
        return _fix_tree(json.loads(matches[0].read_text(encoding="utf-8")))
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = sorted(n for n in zf.namelist() if n.rsplit("/", 1)[-1] == filename)
            if not names:
                return None
            return _fix_tree(json.loads(zf.read(names[0]).decode("utf-8")))
    raise FileNotFoundError(f"폴더나 zip 파일이 아님: {path}")


def _records(data: Any) -> list[Any]:
    """파일 안의 항목 목록.

    최상위가 리스트, {키: 리스트} 하나짜리 dict(옛 형식), dict 하나(컬렉션 1개)인 경우를 다룬다.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(data) == 1 and len(lists) == 1:
            return lists[0]
        return [data]
    return []


def _hrefs(node: Any, ts: int | None = None) -> Iterator[tuple[str, int | None]]:
    """(href, 가장 가까운 상위 timestamp) 를 순서대로 돌려준다."""
    if isinstance(node, dict):
        own = node.get("timestamp")
        if isinstance(own, int) and own > 0:
            ts = own
        for key, value in node.items():
            if key == "href" and isinstance(value, str):
                yield value, ts
            else:
                yield from _hrefs(value, ts)
    elif isinstance(node, list):
        for x in node:
            yield from _hrefs(x, ts)


def _post_links(record: Any) -> list[tuple[str, datetime | None]]:
    """항목 하나에서 인스타 게시물 링크와 저장 시각. 같은 게시물은 한 번만."""
    seen: set[str] = set()
    links: list[tuple[str, datetime | None]] = []
    for href, ts in _hrefs(record):
        parsed = parse_url(href)
        if parsed is None or parsed.platform != "instagram" or parsed.source_id in seen:
            continue
        seen.add(parsed.source_id)
        links.append((href, datetime.fromtimestamp(ts, UTC) if ts else None))
    return links


def _find_label(node: Any, labels: set[str]) -> str | None:
    """{"label": <labels 중 하나>, "value": "..."} 중 값이 비어 있지 않은 첫 번째."""
    if isinstance(node, dict):
        value = node.get("value")
        if node.get("label") in labels and isinstance(value, str) and value.strip():
            return value
        children: Any = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        if (found := _find_label(child, labels)) is not None:
            return found
    return None


def _find_titled(node: Any, titles: set[str]) -> Any | None:
    """{"title": <titles 중 하나>, ...} 인 첫 번째 dict."""
    if isinstance(node, dict):
        if node.get("title") in titles:
            return node
        children: Any = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        if (found := _find_titled(child, titles)) is not None:
            return found
    return None


def _post_meta(record: Any) -> tuple[str | None, str | None]:
    """(캡션, 작성자 아이디). 한 항목에 같은 캡션이 두 번 들어 있기도 해서 첫 번째를 쓴다.

    옛 형식은 항목의 title 이 작성자 아이디다 (캡션은 없음).
    """
    caption = _find_label(record, CAPTION_LABELS)
    owner = _find_titled(record, OWNER_TITLES)
    author = _find_label(owner, USERNAME_LABELS) if owner is not None else None
    if author is None and isinstance(record, dict) and "string_map_data" in record:
        title = record.get("title")
        author = title if isinstance(title, str) and title else None
    return caption, author


def _collection_name(record: Any) -> str | None:
    """가장 얕은 곳의 '이름' 값. 깊은 곳의 '이름'은 작성자·해시태그라서 너비 우선으로 찾는다.

    2026년 형식: {"label": "이름", "value": "..."}, 옛 형식: {"Name": {"value": "..."}}
    """
    queue: deque[Any] = deque([record])
    while queue:
        node = queue.popleft()
        if isinstance(node, dict):
            if node.get("label") in NAME_LABELS and isinstance(node.get("value"), str):
                return node["value"]
            for label in NAME_LABELS:
                inner = node.get(label)
                if isinstance(inner, dict) and isinstance(inner.get("value"), str):
                    return inner["value"]
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


def _collection_map(data: Any) -> dict[str, tuple[str, str]]:
    """shortcode → (링크, 컬렉션 이름). 한 게시물이 여러 컬렉션에 있으면 첫 번째.

    옛 형식은 '컬렉션 머리 항목(이름만)' 뒤에 그 컬렉션의 게시물 항목이 이어진다.
    """
    result: dict[str, tuple[str, str]] = {}
    current: str | None = None
    for record in _records(data):
        links = _post_links(record)
        name = _collection_name(record)
        if not links:
            current = name or current
            continue
        name = name or current
        if name is None:
            logger.warning("컬렉션 이름을 못 찾음: 게시물 %d개", len(links))
            continue
        for href, _ in links:
            parsed = parse_url(href)
            assert parsed is not None
            result.setdefault(parsed.source_id, (href, name))
    return result


def import_instagram(
    conn: sqlite3.Connection, path: Path, *, limit: int | None = None
) -> ImportReport:
    """내보내기 폴더 또는 zip 을 가져온다. limit 은 처리할 항목(게시물) 수."""
    posts = _load_json(path, POSTS_FILE)
    if posts is None:
        raise FileNotFoundError(f"{POSTS_FILE} 을 찾지 못함: {path}")
    collections_data = _load_json(path, COLLECTIONS_FILE)
    collections = _collection_map(collections_data) if collections_data is not None else {}

    report = ImportReport()
    done: set[str] = set()
    processed = 0
    with conn:  # 하나의 트랜잭션: 성공하면 커밋, 예외면 롤백
        for i, record in enumerate(_records(posts)):
            if limit is not None and processed >= limit:
                break
            processed += 1
            links = _post_links(record)
            if not links:
                logger.warning("%s %d번째 항목에서 게시물 링크를 못 찾음", POSTS_FILE, i + 1)
                report.unrecognized += 1
                continue
            # 저장 목록의 항목 하나 = 게시물 하나라서 캡션·작성자는 그 게시물 것이다
            caption, author = _post_meta(record) if len(links) == 1 else (None, None)
            for href, saved_at in links:
                parsed = parse_url(href)
                assert parsed is not None
                collection = collections.get(parsed.source_id, (None, None))[1]
                report.record(
                    db.add_item(
                        conn,
                        href,
                        saved_at=saved_at,
                        source=SOURCE,
                        collection=collection,
                        description=caption,
                        author=author,
                    )
                )
                done.add(parsed.source_id)

        # 컬렉션에만 있는 게시물 (저장 시각은 모름)
        for shortcode, (href, name) in collections.items():
            if shortcode in done:
                continue
            if limit is not None and processed >= limit:
                break
            processed += 1
            report.record(db.add_item(conn, href, saved_at=None, source=SOURCE, collection=name))
    return report
