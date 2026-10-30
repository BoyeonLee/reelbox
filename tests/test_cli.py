import pytest
from typer.testing import CliRunner

from reelbox import cli
from reelbox.config import Settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def use_tmp_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


def test_status_without_db_suggests_init() -> None:
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 1
    assert "reelbox init" in result.output


def test_init_then_status(settings: Settings) -> None:
    first = runner.invoke(cli.app, ["init"])
    assert first.exit_code == 0, first.output
    assert "001_init.sql" in first.output
    assert settings.db_path.is_file()

    second = runner.invoke(cli.app, ["init"])
    assert second.exit_code == 0
    assert "이미 최신" in second.output

    status = runner.invoke(cli.app, ["status"])
    assert status.exit_code == 0, status.output
    for name in ("imported", "indexed", "failed", "합계"):
        assert name in status.output
    assert "최근 실패 없음" in status.output


@pytest.mark.parametrize(
    "args",
    [
        ["import", "youtube", "x"],
        ["enrich", "--limit", "5"],
        ["analyze", "--dry-run"],
        ["organize", "--full"],
        ["index"],
        ["run"],
        ["search", "파스타"],
        ["serve"],
        ["purge"],
        ["publish", "--dry-run"],
    ],
)
def test_unimplemented_commands(args: list[str]) -> None:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "아직 구현 안 됨" in result.output
