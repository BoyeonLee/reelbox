"""import / sync 명령 테스트. yt-dlp 는 youtube.fetch_playlist 를 가짜로 바꾼다."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from reelbox import cli, db
from reelbox.config import Settings
from reelbox.importers import youtube
from reelbox.importers.youtube import CookieError, CookieSource, PlaylistEntry

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


@pytest.fixture
def yt_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "youtube_cookies_from_browser": "firefox",
            "youtube_saved_shorts_url": "https://www.youtube.com/playlist?list=YS",
        }
    )


@pytest.fixture(autouse=True)
def use_tmp_settings(yt_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: yt_settings)


@pytest.fixture
def initialized(yt_settings: Settings) -> Settings:
    db.init_db(yt_settings)
    return yt_settings


def _count(settings: Settings) -> int:
    conn = db.connect(settings.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()


def _fake_playlist(monkeypatch: pytest.MonkeyPatch, ids: list[str]) -> list[dict]:
    calls: list[dict] = []

    def fake(url: str, cookies: CookieSource, limit: int | None) -> list[PlaylistEntry]:
        calls.append({"url": url, "cookies": cookies, "limit": limit})
        return [PlaylistEntry(video_id=i, title=f"제목 {i}") for i in ids][:limit]

    monkeypatch.setattr(youtube, "fetch_playlist", fake)
    return calls


def test_import_needs_db() -> None:
    result = runner.invoke(cli.app, ["import", "urls", str(FIXTURES / "urls.txt")])
    assert result.exit_code == 1
    assert "reelbox init" in result.output


def test_import_instagram_twice(initialized: Settings) -> None:
    first = runner.invoke(cli.app, ["import", "instagram", str(FIXTURES / "instagram")])
    assert first.exit_code == 0, first.output
    assert "새로 추가 4, 중복 1, 인식 실패 1" in first.output

    again = runner.invoke(cli.app, ["import", "instagram", str(FIXTURES / "instagram")])
    assert again.exit_code == 0, again.output
    assert "새로 추가 0, 중복 5, 인식 실패 1" in again.output
    assert _count(initialized) == 4


def test_import_instagram_missing_file(initialized: Settings, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["import", "instagram", str(tmp_path)])
    assert result.exit_code == 1
    assert "saved_posts.json" in result.output


def test_import_urls_with_limit(initialized: Settings) -> None:
    result = runner.invoke(cli.app, ["import", "urls", str(FIXTURES / "urls.txt"), "--limit", "3"])
    assert result.exit_code == 0, result.output
    assert "새로 추가 2, 중복 1, 인식 실패 0" in result.output


def test_sync_dry_run_shows_entries_and_writes_nothing(
    initialized: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_playlist(monkeypatch, [f"dry{i:08d}" for i in range(20)])
    result = runner.invoke(cli.app, ["sync", "youtube", "--limit", "10", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert calls[0]["limit"] == 10
    assert calls[0]["url"] == "https://www.youtube.com/playlist?list=YS"
    assert "dry00000000" in result.output
    assert "dry00000009" in result.output
    assert "dry00000010" not in result.output
    assert "DB에 쓰지 않음" in result.output
    assert _count(initialized) == 0


def test_sync_twice_adds_only_new(initialized: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_playlist(monkeypatch, ["old00000001", "old00000002"])
    first = runner.invoke(cli.app, ["sync", "youtube"])
    assert first.exit_code == 0, first.output
    assert "새로 추가 2, 중복 0, 인식 실패 0" in first.output

    _fake_playlist(monkeypatch, ["new00000003", "old00000001", "old00000002"])
    second = runner.invoke(cli.app, ["sync", "youtube"])
    assert second.exit_code == 0, second.output
    assert "새로 추가 1, 중복 2, 인식 실패 0" in second.output
    assert _count(initialized) == 3


def test_sync_cookie_error_explains_and_stops(
    initialized: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(url: str, cookies: CookieSource, limit: int | None) -> list[PlaylistEntry]:
        raise CookieError(cause="원인 설명", fix="해결 방법", detail="ERROR: raw")

    monkeypatch.setattr(youtube, "fetch_playlist", fake)
    result = runner.invoke(cli.app, ["sync", "youtube", "--limit", "10"])
    assert result.exit_code == 1
    for part in ("원인 설명", "해결 방법", "ERROR: raw"):
        assert part in result.output
    assert _count(initialized) == 0


def test_sync_without_cookie_setting(
    initialized: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_cookie = initialized.model_copy(update={"youtube_cookies_from_browser": None})
    monkeypatch.setattr(cli, "get_settings", lambda: no_cookie)
    calls = _fake_playlist(monkeypatch, ["x0000000001"])
    result = runner.invoke(cli.app, ["sync", "youtube"])
    assert result.exit_code == 1
    assert "REELBOX_YOUTUBE_COOKIES" in result.output
    assert calls == []


def test_sync_without_playlist_url(initialized: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    no_url = initialized.model_copy(update={"youtube_saved_shorts_url": None})
    monkeypatch.setattr(cli, "get_settings", lambda: no_url)
    result = runner.invoke(cli.app, ["sync", "youtube"])
    assert result.exit_code == 1
    assert "REELBOX_YOUTUBE_SAVED_SHORTS_URL" in result.output
