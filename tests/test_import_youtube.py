"""유튜브 동기화 테스트. yt-dlp 는 youtube._extract 를 가짜로 바꿔서 실제로 호출하지 않는다."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from yt_dlp.utils import DownloadError

from reelbox.config import PROJECT_ROOT, Settings
from reelbox.importers import ImportReport, youtube
from reelbox.importers.youtube import CookieError, CookieSource, PlaylistEntry

URL = "https://www.youtube.com/playlist?list=YS"
NOW = datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC)


def _ids(n: int, prefix: str = "vid") -> list[str]:
    return [f"{prefix}{i:08d}" for i in range(n)]  # 11글자 가짜 ID


# --- 쿠키 설정 ---


def test_cookie_source_needs_one_setting() -> None:
    with pytest.raises(CookieError) as e:
        CookieSource.from_settings(Settings(_env_file=None))
    assert "REELBOX_YOUTUBE_COOKIES" in e.value.fix

    with pytest.raises(CookieError) as e:
        CookieSource.from_settings(
            Settings(
                _env_file=None,
                youtube_cookies_from_browser="firefox",
                youtube_cookies_file=Path("x.txt"),
            )
        )
    assert "하나만" in e.value.cause


def test_cookie_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.txt"
    with pytest.raises(CookieError) as e:
        CookieSource.from_settings(Settings(_env_file=None, youtube_cookies_file=missing))
    assert str(missing) in e.value.cause


def test_cookie_file_relative_to_project_root(tmp_path: Path) -> None:
    src = CookieSource.from_settings(
        Settings(_env_file=None, youtube_cookies_file=Path("tests/test_import_youtube.py"))
    )
    assert src.ydl_opts() == {"cookiefile": str(PROJECT_ROOT / "tests/test_import_youtube.py")}


def test_cookie_browser_with_profile() -> None:
    src = CookieSource.from_settings(
        Settings(_env_file=None, youtube_cookies_from_browser="firefox:default-release")
    )
    assert src.ydl_opts() == {"cookiesfrombrowser": ("firefox", "default-release", None, None)}


def test_empty_env_values_are_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # .env.example 처럼 'KEY=' 로 비워 둔 값은 설정 안 함으로 본다
    env = tmp_path / ".env"
    env.write_text("REELBOX_YOUTUBE_COOKIES_FROM_BROWSER=\nREELBOX_YOUTUBE_COOKIES_FILE=c.txt\n")
    s = Settings(_env_file=env)
    assert s.youtube_cookies_from_browser is None
    assert s.youtube_cookies_file == Path("c.txt")


# --- yt-dlp 래퍼 ---

COOKIES = CookieSource(browser="firefox")


def test_fetch_playlist_options_and_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_extract(url: str, opts: dict[str, Any]) -> dict[str, Any]:
        seen.update(url=url, opts=opts)
        return {
            "entries": [
                {
                    "id": "aaaaaaaaaaa",
                    "title": "첫 영상",
                    "url": "https://www.youtube.com/shorts/aaaaaaaaaaa",
                },
                None,  # 볼 수 없는 항목
                {"id": "bbbbbbbbbbb", "title": None},
                {"title": "ID 없음"},
            ]
        }

    monkeypatch.setattr(youtube, "_extract", fake_extract)
    entries = youtube.fetch_playlist(URL, COOKIES, limit=10)

    assert entries == [
        PlaylistEntry(video_id="aaaaaaaaaaa", title="첫 영상"),
        PlaylistEntry(video_id="bbbbbbbbbbb", title=None),
    ]
    assert seen["url"] == URL
    assert seen["opts"]["extract_flat"] == "in_playlist"
    assert seen["opts"]["skip_download"] is True
    assert seen["opts"]["playlistend"] == 10
    assert seen["opts"]["cookiesfrombrowser"] == ("firefox", None, None, None)


def test_fetch_playlist_without_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(youtube, "_extract", lambda url, opts: seen.update(opts) or {"entries": []})
    assert youtube.fetch_playlist(URL, COOKIES, limit=None) == []
    assert "playlistend" not in seen


@pytest.mark.parametrize(
    ("message", "cause_part"),
    [
        (
            "ERROR: could not find firefox cookies database in /home/u/.mozilla/firefox",
            "브라우저 쿠키를 읽지 못함",
        ),
        ("ERROR: Failed to decrypt with DPAPI", "브라우저 쿠키를 읽지 못함"),
        (
            "ERROR: 'c.txt' does not look like a Netscape format cookies file",
            "cookies.txt 형식",
        ),
        ("ERROR: [youtube:tab] YS: The playlist does not exist.", "로그인"),
        ("ERROR: [youtube:tab] Sign in to confirm you’re not a bot", "로그인"),
    ],
)
def test_cookie_errors_are_explained(
    monkeypatch: pytest.MonkeyPatch, message: str, cause_part: str
) -> None:
    def fake_extract(url: str, opts: dict[str, Any]) -> dict[str, Any]:
        raise DownloadError(message)

    monkeypatch.setattr(youtube, "_extract", fake_extract)
    with pytest.raises(CookieError) as e:
        youtube.fetch_playlist(URL, COOKIES, limit=10)
    assert cause_part in e.value.cause
    assert e.value.fix
    assert message in e.value.detail


def test_other_errors_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_extract(url: str, opts: dict[str, Any]) -> dict[str, Any]:
        raise DownloadError("ERROR: Unable to download API page: HTTP Error 503")

    monkeypatch.setattr(youtube, "_extract", fake_extract)
    with pytest.raises(youtube.PlaylistError, match="503"):
        youtube.fetch_playlist(URL, COOKIES, limit=None)


# --- DB 에 넣기 ---


def _entries(ids: list[str]) -> list[PlaylistEntry]:
    return [PlaylistEntry(video_id=i, title=f"t-{i}") for i in ids]


def test_add_entries_oldest_first(conn: sqlite3.Connection) -> None:
    ids = _ids(3)  # 목록은 최신순
    report = youtube.add_entries(conn, _entries(ids), now=NOW)
    assert report == ImportReport(added=3)
    rows = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    # 오래된 것부터 넣어서 id 순서 = 저장 순서
    assert [r["source_id"] for r in rows] == list(reversed(ids))
    assert {r["saved_at"] for r in rows} == {"2026-10-30T09:00:00Z"}  # 처음 발견한 시각
    assert {r["imported_from"] for r in rows} == {"youtube_sync"}
    assert rows[0]["url"] == f"https://www.youtube.com/shorts/{ids[-1]}"


def test_resync_adds_only_new(conn: sqlite3.Connection) -> None:
    old = _ids(3)
    youtube.add_entries(conn, _entries(old), now=NOW)

    new = _ids(2, prefix="new")
    later = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
    report = youtube.add_entries(conn, _entries(new + old), now=later)
    assert report == ImportReport(added=2, duplicate=3)

    rows = conn.execute("SELECT source_id, saved_at FROM items ORDER BY id").fetchall()
    assert len(rows) == 5
    # 기존 항목의 처음 발견 시각은 바뀌지 않는다
    assert {r["saved_at"] for r in rows if r["source_id"] in old} == {"2026-10-30T09:00:00Z"}
    assert [r["source_id"] for r in rows[3:]] == list(reversed(new))

    assert youtube.add_entries(conn, _entries(new + old), now=later) == ImportReport(duplicate=5)
