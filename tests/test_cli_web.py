"""reelbox purge, reelbox serve (서버는 띄우지 않고 시작 전 검사만)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from reelbox import cli, db
from reelbox.config import Settings

from .conftest import analyzed_item

runner = CliRunner()


@pytest.fixture(autouse=True)
def use_tmp_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


def test_purge_command(conn: sqlite3.Connection) -> None:
    old = analyzed_item(conn, "aaaaaaaaaaa", title="오래된 것")
    new = analyzed_item(conn, "bbbbbbbbbbb", title="최근 것")
    now = datetime.now(UTC)
    db.trash_items(conn, [old], now=now - timedelta(days=31))
    db.trash_items(conn, [new], now=now - timedelta(days=1))

    result = runner.invoke(cli.app, ["purge"])
    assert result.exit_code == 0, result.output
    assert "영구 정리 1개" in result.output
    assert runner.invoke(cli.app, ["purge"]).output.count("영구 정리 0개") == 1
    assert "영구 정리 1개" in runner.invoke(cli.app, ["purge", "--days", "0"]).output


def test_serve_requires_token(conn: sqlite3.Connection) -> None:
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 1
    assert "REELBOX_TOKEN" in result.output
