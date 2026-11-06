"""enrich 명령 테스트. YtdlpClient·WhisperTranscriber 를 가짜로 바꿔서 네트워크 없이 돈다."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from reelbox import cli, db
from reelbox.config import Settings
from reelbox.enrich.transcribe import Transcript
from reelbox.ytdlp import CookieSource

from .conftest import insert_item
from .test_enrich_runner import FakeClient, yt_info

runner = CliRunner()


@pytest.fixture(autouse=True)
def use_tmp_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[CookieSource | None]:
    """YtdlpClient 를 가짜로 바꾸고, 어떤 쿠키로 열었는지 기록한다."""
    seen: list[CookieSource | None] = []
    responses: dict[str, Any] = {
        "https://www.youtube.com/shorts/aaaaaaaaaaa": yt_info("aaaaaaaaaaa")
    }

    def fake_client(cookies: CookieSource | None) -> FakeClient:
        seen.append(cookies)
        return FakeClient(responses, [])

    class FakeWhisper:
        def __init__(self, *args: object) -> None:
            pass

        def __call__(self, audio_path: Path) -> Transcript:
            return Transcript(text="말", language="ko")

    monkeypatch.setattr(cli, "YtdlpClient", fake_client)
    monkeypatch.setattr(cli, "WhisperTranscriber", FakeWhisper)
    return seen


def _setup(settings: Settings) -> sqlite3.Connection:
    db.init_db(settings)
    conn = db.connect(settings.db_path)
    insert_item(conn, "aaaaaaaaaaa", media_type="video")
    insert_item(
        conn,
        "REEL1",
        platform="instagram",
        url="https://www.instagram.com/reel/REEL1/",
        description="내보내기 캡션",
    )
    return conn


def test_enrich_youtube_without_cookies_and_instagram_stops(
    settings: Settings, opened: list[CookieSource | None]
) -> None:
    conn = _setup(settings)
    result = runner.invoke(cli.app, ["enrich", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "보강 결과" in result.output
    assert "instagram 처리를 멈춤" in result.output
    assert "REELBOX_INSTAGRAM_COOKIES" in result.output
    assert opened == [None]  # 유튜브는 쿠키 설정이 없으면 쿠키 없이 연다
    assert db.status_counts(conn)["enriched"] == 1
    conn.close()


def test_enrich_platform_option_rejects_unknown(settings: Settings) -> None:
    db.init_db(settings)
    result = runner.invoke(cli.app, ["enrich", "--platform", "tiktok"])
    assert result.exit_code != 0


def test_enrich_captions_only(settings: Settings, opened: list[CookieSource | None]) -> None:
    conn = _setup(settings)
    result = runner.invoke(cli.app, ["enrich", "--captions-only", "--platform", "instagram"])
    assert result.exit_code == 0, result.output
    assert opened == []  # 네트워크 클라이언트를 열지 않는다
    row = conn.execute("SELECT status FROM items WHERE source_id = 'REEL1'").fetchone()
    assert row["status"] == "enriched"
    conn.close()
