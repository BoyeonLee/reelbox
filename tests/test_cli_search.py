"""organize / index / search 명령. LLM 과 임베딩 모델은 가짜로 바꾼다."""

import pytest
from rich.console import Console
from typer.testing import CliRunner

from reelbox import cli, db
from reelbox.config import Settings

from .conftest import FakeEmbedder, analyzed_item
from .test_organize import SchemaLLM, default_llm

runner = CliRunner()


@pytest.fixture(autouse=True)
def use_tmp_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "console", Console(width=200))  # 표가 줄바꿈되지 않게


@pytest.fixture
def embedder(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    fake = FakeEmbedder()
    monkeypatch.setattr(cli, "_make_embedder", lambda settings: fake)
    return fake


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> SchemaLLM:
    fake = default_llm()
    monkeypatch.setattr(cli, "_make_organize_llm", lambda settings: fake)
    return fake


@pytest.fixture
def seeded(settings: Settings) -> Settings:
    db.init_db(settings)
    conn = db.connect(settings.db_path)
    analyzed_item(
        conn, "aaaaaaaaaaa", title="브라운버터 파스타", raw_topic="파스타 레시피", tags=["파스타"]
    )
    analyzed_item(conn, "bbbbbbbbbbb", title="서울 김밥 맛집", raw_topic="김밥 레시피")
    analyzed_item(conn, "ccccccccccc", title="눈화장 팁", raw_topic="아이메이크업")
    conn.close()
    return settings


def test_organize_index_search(seeded: Settings, llm: SchemaLLM, embedder: FakeEmbedder) -> None:
    result = runner.invoke(cli.app, ["organize"])
    assert result.exit_code == 0, result.output
    assert "전체 정리" in result.output
    assert "주제 2개" in result.output
    assert "요리" in result.output and "뷰티" in result.output

    result = runner.invoke(cli.app, ["index"])
    assert result.exit_code == 0, result.output
    assert "색인 결과" in result.output

    result = runner.invoke(cli.app, ["search", "파스타"])
    assert result.exit_code == 0, result.output
    assert "브라운버터 파스타" in result.output
    assert "키워드 1위" in result.output
    assert "의미" in result.output

    result = runner.invoke(cli.app, ["search", "김밥", "--topic", "뷰티", "--keyword-only"])
    assert result.exit_code == 0, result.output
    assert "결과 없음" in result.output


def test_organize_twice_is_incremental(
    seeded: Settings, llm: SchemaLLM, embedder: FakeEmbedder
) -> None:
    runner.invoke(cli.app, ["organize"])
    result = runner.invoke(cli.app, ["organize"])
    assert result.exit_code == 0, result.output
    assert "새 항목 배정" in result.output


def test_search_keyword_only_does_not_make_embedder(
    seeded: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(settings: Settings) -> None:
        raise AssertionError("모델을 만들면 안 됨")

    monkeypatch.setattr(cli, "_make_embedder", boom)
    result = runner.invoke(cli.app, ["search", "파스타", "--keyword-only"])
    assert result.exit_code == 0, result.output


def test_search_bad_date(seeded: Settings, embedder: FakeEmbedder) -> None:
    result = runner.invoke(cli.app, ["search", "파스타", "--since", "2026/01/01"])
    assert result.exit_code == 1
    assert "YYYY-MM-DD" in result.output


def test_organize_needs_api_key(seeded: Settings) -> None:
    result = runner.invoke(cli.app, ["organize"])
    assert result.exit_code == 1
    assert "API 키가 없음" in result.output
