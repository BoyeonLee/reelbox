"""analyze / review 명령. LLM 은 가짜로 바꾼다."""

import pytest
from rich.console import Console
from typer.testing import CliRunner

from reelbox import cli, db
from reelbox.ai.llm import LLMFatalError
from reelbox.config import Settings

from .test_analyze import GOOD, FakeLLM, _item

runner = CliRunner()


@pytest.fixture(autouse=True)
def use_tmp_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM(GOOD)
    monkeypatch.setattr(cli, "_make_llm", lambda settings: fake)
    return fake


@pytest.fixture
def seeded(settings: Settings) -> Settings:
    db.init_db(settings)
    conn = db.connect(settings.db_path)
    _item(conn, "aaaaaaaaaaa", title="김치찌개 3분 레시피", author="요리왕")
    _item(conn, "bbbbbbbbbbb")
    conn.close()
    return settings


def test_dry_run_shows_estimate_and_writes_nothing(seeded: Settings, llm: FakeLLM) -> None:
    result = runner.invoke(cli.app, ["analyze", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "분석할 항목" in result.output
    assert "2개" in result.output
    assert "예상 비용" in result.output
    conn = db.connect(seeded.db_path)
    assert db.status_counts(conn)["analyzed"] == 0
    conn.close()


def test_analyze_then_review(
    seeded: Settings, llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "console", Console(width=200))  # 표가 줄바꿈되지 않게
    result = runner.invoke(cli.app, ["analyze", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert "분석 결과" in result.output

    review = runner.invoke(cli.app, ["review", "--limit", "10"])
    assert review.exit_code == 0, review.output
    assert "김치찌개 3분 레시피" in review.output
    assert "파스타 레시피" in review.output
    assert "브라운버터" in review.output


def test_review_without_analyses(seeded: Settings) -> None:
    result = runner.invoke(cli.app, ["review"])
    assert result.exit_code == 0
    assert "reelbox analyze" in result.output


def test_dry_run_api_error_exits(seeded: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLLM(GOOD)

    def broken(**kwargs: object) -> int:
        raise LLMFatalError("Claude API 401: invalid x-api-key")

    fake.count_tokens = broken  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "_make_llm", lambda settings: fake)
    result = runner.invoke(cli.app, ["analyze", "--dry-run"])
    assert result.exit_code == 1
    assert "401" in result.output


def test_analyze_without_api_key_explains(seeded: Settings) -> None:
    result = runner.invoke(cli.app, ["analyze", "--dry-run"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
