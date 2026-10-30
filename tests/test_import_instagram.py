import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from reelbox.importers import ImportReport
from reelbox.importers.instagram import fix_mojibake, import_instagram

FIXTURES = Path(__file__).parent / "fixtures"
EXPORT = FIXTURES / "instagram"


def _items(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["source_id"]: r for r in conn.execute("SELECT * FROM items")}


def test_fix_mojibake() -> None:
    broken = "캡션".encode().decode("latin-1")
    assert broken != "캡션"
    assert fix_mojibake(broken) == "캡션"
    assert fix_mojibake("캡션") == "캡션"  # 이미 정상인 글자는 그대로
    assert fix_mojibake("URL") == "URL"
    assert fix_mojibake("café") == "café"  # latin-1 이지만 UTF-8 바이트가 아님


def test_import_fixture(conn: sqlite3.Connection) -> None:
    report = import_instagram(conn, EXPORT)
    assert report == ImportReport(added=4, duplicate=1, unrecognized=1)

    items = _items(conn)
    assert set(items) == {"FAKEreel001", "FAKEpost002", "FAKEreel003", "FAKEreel004"}
    reel = items["FAKEreel001"]
    assert reel["platform"] == "instagram"
    assert reel["url"] == "https://www.instagram.com/reel/FAKEreel001/"
    assert reel["saved_at"] == "2026-10-27T11:20:00Z"  # 1793100000
    assert reel["imported_from"] == "instagram_export"
    assert reel["collection"] == "요리 레시피"  # 깨진 글자 복원
    assert reel["media_type"] == "video"
    assert items["FAKEreel003"]["url"] == "https://www.instagram.com/reel/FAKEreel003/"
    assert items["FAKEpost002"]["media_type"] is None
    assert items["FAKEpost002"]["collection"] is None


def test_reimport_adds_nothing(conn: sqlite3.Connection) -> None:
    import_instagram(conn, EXPORT)
    again = import_instagram(conn, EXPORT)
    assert again == ImportReport(added=0, duplicate=5, unrecognized=1)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4


def test_limit(conn: sqlite3.Connection) -> None:
    report = import_instagram(conn, EXPORT, limit=2)
    assert report.added == 2
    assert set(_items(conn)) == {"FAKEreel001", "FAKEpost002"}


def test_import_from_zip(conn: sqlite3.Connection, tmp_path: Path) -> None:
    archive = shutil.make_archive(str(tmp_path / "export"), "zip", EXPORT)
    report = import_instagram(conn, Path(archive))
    assert report.added == 4
    assert _items(conn)["FAKEreel001"]["collection"] == "요리 레시피"


def test_legacy_format(conn: sqlite3.Connection) -> None:
    report = import_instagram(conn, FIXTURES / "instagram_legacy")
    assert report == ImportReport(added=2)
    items = _items(conn)
    assert items["LEGACYreel1"]["saved_at"] == "2023-11-14T22:13:20Z"
    assert items["LEGACYpost2"]["url"] == "https://www.instagram.com/p/LEGACYpost2/"


def _collection(name: str, *shortcodes: str) -> dict:
    return {
        "timestamp": 1792950000,
        "label_values": [
            {"label": "이름".encode().decode("latin-1"), "value": name},
            {
                "title": "미디어",
                "dict": [
                    {
                        "title": "",
                        "dict": [
                            {"label": "URL", "href": f"https://www.instagram.com/reel/{s}/"},
                            # 작성자 이름도 '이름' 라벨이지만 더 깊어서 컬렉션 이름이 아니다
                            {"title": "소유자", "dict": [{"label": "이름", "value": "작성자"}]},
                        ],
                    }
                    for s in shortcodes
                ],
            },
        ],
    }


def test_multiple_collections(conn: sqlite3.Connection, tmp_path: Path) -> None:
    saved = tmp_path / "export" / "saved"
    saved.mkdir(parents=True)
    posts = [
        {"timestamp": 1793000000, "label_values": [{"href": f"https://instagram.com/reel/{s}/"}]}
        for s in ("COLLaaaa001", "COLLbbbb002")
    ]
    (saved / "saved_posts.json").write_text(json.dumps(posts))
    collections = [
        _collection("운동", "COLLaaaa001"),
        _collection("여행", "COLLbbbb002", "COLLaaaa001", "ONLYcoll003"),
    ]
    (saved / "saved_collections.json").write_text(json.dumps(collections))

    report = import_instagram(conn, tmp_path / "export")
    assert report.added == 3
    items = _items(conn)
    assert items["COLLaaaa001"]["collection"] == "운동"  # 여러 개면 첫 번째
    assert items["COLLbbbb002"]["collection"] == "여행"
    # 컬렉션에만 있는 항목도 넣는다 (저장 시각은 모름)
    assert items["ONLYcoll003"]["collection"] == "여행"
    assert items["ONLYcoll003"]["saved_at"] is None


def test_missing_saved_posts(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="saved_posts.json"):
        import_instagram(conn, tmp_path)
