"""publish 명령. Cloudflare 확인·wrangler 는 가짜로 바꾼다."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from reelbox import cli, db
from reelbox import publish as pub
from reelbox.config import Settings

from .conftest import analyzed_item

runner = CliRunner()


@pytest.fixture
def pages(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    configured = settings.model_copy(
        update={"pages_project": "reelbox", "pages_url": "https://reelbox.pages.dev"}
    )
    monkeypatch.setattr(cli, "get_settings", lambda: configured)
    db.init_db(configured)
    conn = db.connect(configured.db_path)
    analyzed_item(conn, "aaaaaaaaaaa", title="브라운버터 파스타")
    conn.close()
    deployed: list[Path] = []
    monkeypatch.setattr(pub, "http_get", lambda url: (302, "https://me.cloudflareaccess.com/x"))
    monkeypatch.setattr(pub, "wrangler_deploy", lambda dist, s: deployed.append(dist))
    return deployed


def test_dry_run_prints_counts_only(pages: list[Path]) -> None:
    result = runner.invoke(cli.app, ["publish", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "영상 1개" in result.output
    assert "파일" in result.output
    assert pages == []


def test_publish_then_unchanged(pages: list[Path]) -> None:
    first = runner.invoke(cli.app, ["publish"])
    assert first.exit_code == 0, first.output
    assert "배포 완료" in first.output
    second = runner.invoke(cli.app, ["publish"])
    assert "올리지 않음" in second.output
    assert len(pages) == 1


def test_access_off_exits_with_error(pages: list[Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pub, "http_get", lambda url: (200, None))
    result = runner.invoke(cli.app, ["publish"])
    assert result.exit_code == 1
    assert "Access" in result.output
    assert pages == []
