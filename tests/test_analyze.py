"""analyze 흐름. LLM 은 가짜라 Claude API 를 부르지 않는다."""

import json
import sqlite3
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from reelbox import db
from reelbox.ai.analyze import (
    AnalysisOutput,
    analyze_items,
    build_user_message,
    dry_run,
    load_prompt,
)
from reelbox.ai.llm import (
    LLMError,
    LLMFatalError,
    LLMOutputError,
    LLMRefusal,
    LLMUnavailableError,
    StructuredResult,
    Usage,
)
from reelbox.config import PROJECT_ROOT, Settings

from .conftest import insert_item

GOOD = AnalysisOutput(
    summary_ko="브라운 버터로 파스타를 만드는 영상이다.",
    key_points=["버터를 갈색이 될 때까지 녹인다", "면수를 넣는다"],
    raw_topic="파스타 레시피",
    tags=["#파스타", "브라운버터", "파스타", " 자취요리 "],
    content_language="ko",
)


class FakeLLM:
    """responses 를 차례로 돌려준다. 예외면 raise."""

    model = "fake-model"

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def structured(self, *, system: str, user: str, schema: type[BaseModel]) -> StructuredResult:
        self.calls.append(user)
        value = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(value, Exception):
            raise value
        return StructuredResult(output=value, usage=Usage(input_tokens=1000, output_tokens=200))

    def count_tokens(self, *, system: str, user: str, schema: type[BaseModel]) -> int:
        self.calls.append(user)
        return 1000 + len(user)


def _item(
    conn: sqlite3.Connection, source_id: str, *, text: str | None = "자막 내용", **cols: Any
) -> int:
    cols.setdefault("status", "enriched")
    item_id = insert_item(conn, source_id, **cols)
    conn.execute(
        "INSERT INTO transcripts (item_id, source, language, text) VALUES (?, ?, 'ko', ?)",
        (item_id, "captions" if text else "none", text),
    )
    conn.commit()
    return item_id


def _row(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def _analysis(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM analyses WHERE item_id = ?", (item_id,)).fetchone()


# --- 프롬프트·입력 ---


def test_load_prompt_strips_header() -> None:
    prompt = load_prompt(PROJECT_ROOT / "prompts", "analyze_v1")
    assert prompt.version == "analyze_v1"
    assert not prompt.system.startswith("---")
    assert "changes:" not in prompt.system
    assert "summary_ko" in prompt.system


def test_build_user_message_truncates_transcript(conn: sqlite3.Connection) -> None:
    _item(
        conn,
        "aaaaaaaaaaa",
        text="가" * 50,
        title="김치찌개",
        author="요리왕",
        description="설명",
        collection="요리",
        media_type="video",
    )
    item = db.analyze_candidates(conn, max_attempts=3, limit=None)[0]
    message = build_user_message(item, transcript_max_chars=10)
    assert message.startswith("<item>") and message.endswith("</item>")
    assert "플랫폼: 유튜브" in message
    assert "제목: 김치찌개" in message
    assert "작성자: 요리왕" in message
    assert "사용자가 넣은 컬렉션: 요리" in message
    assert "자막:\n" + "가" * 10 + "\n…(뒷부분 생략)" in message


def test_build_user_message_without_content(conn: sqlite3.Connection) -> None:
    _item(conn, "bbbbbbbbbbb", text=None, media_type="image")
    item = db.analyze_candidates(conn, max_attempts=3, limit=None)[0]
    message = build_user_message(item, 6000)
    assert "종류: 사진 게시물" in message
    assert "설명:\n(없음)" in message
    assert "자막: (없음)" in message


def test_output_validation() -> None:
    assert GOOD.tags == ["파스타", "브라운버터", "자취요리"]  # # 제거·중복 제거
    with pytest.raises(ValidationError):
        AnalysisOutput(**{**GOOD.model_dump(), "tags": []})
    with pytest.raises(ValidationError):
        AnalysisOutput(**{**GOOD.model_dump(), "summary_ko": ""})


# --- 분석 ---


def test_analyze_saves_and_marks_analyzed(conn: sqlite3.Connection, settings: Settings) -> None:
    item_id = _item(conn, "ccccccccccc")
    llm = FakeLLM(GOOD)
    report = analyze_items(conn, settings, llm)

    assert report.analyzed == 1
    assert report.usage == Usage(input_tokens=1000, output_tokens=200)
    assert _row(conn, item_id)["status"] == "analyzed"
    a = _analysis(conn, item_id)
    assert a["summary_ko"] == GOOD.summary_ko
    assert json.loads(a["tags"]) == ["파스타", "브라운버터", "자취요리"]
    assert json.loads(a["key_points"]) == GOOD.key_points
    assert (a["raw_topic"], a["content_language"]) == ("파스타 레시피", "ko")
    assert (a["model"], a["prompt_version"]) == ("fake-model", "analyze_v1")


def test_idempotent_and_limit(conn: sqlite3.Connection, settings: Settings) -> None:
    for i in range(3):
        _item(conn, f"ddddddddd{i:02d}")
    llm = FakeLLM(GOOD)
    assert analyze_items(conn, settings, llm, limit=2).analyzed == 2
    assert analyze_items(conn, settings, llm).analyzed == 1
    assert analyze_items(conn, settings, llm).analyzed == 0
    assert len(llm.calls) == 3


def test_output_error_is_retried_once(conn: sqlite3.Connection, settings: Settings) -> None:
    item_id = _item(conn, "eeeeeeeeeee")
    llm = FakeLLM(LLMOutputError("JSON 아님"), GOOD)
    report = analyze_items(conn, settings, llm)
    assert (report.analyzed, report.retried) == (1, 1)
    assert _row(conn, item_id)["status"] == "analyzed"


def test_output_error_twice_fails_item(conn: sqlite3.Connection, settings: Settings) -> None:
    first = _item(conn, "fffffffffff")
    second = _item(conn, "ggggggggggg")
    llm = FakeLLM(LLMOutputError("JSON 아님"), LLMOutputError("또 아님"), GOOD)
    report = analyze_items(conn, settings, llm)
    assert (report.failed, report.analyzed) == (1, 1)
    row = _row(conn, first)
    assert (row["status"], row["attempts"], row["error"]) == ("failed", 1, "또 아님")
    assert _row(conn, second)["status"] == "analyzed"  # 다음 항목은 계속


def test_refusal_is_not_retried(conn: sqlite3.Connection, settings: Settings) -> None:
    item_id = _item(conn, "hhhhhhhhhhh")
    llm = FakeLLM(LLMRefusal("모델이 거절함 (cyber)"), GOOD)
    report = analyze_items(conn, settings, llm)
    assert (report.failed, report.retried) == (1, 0)
    assert "거절" in _row(conn, item_id)["error"]


@pytest.mark.parametrize(
    "error", [LLMFatalError("Claude API 401: 키"), LLMUnavailableError("Claude API 429: 많음")]
)
def test_fatal_or_unavailable_stops_without_blaming_item(
    conn: sqlite3.Connection, settings: Settings, error: Exception
) -> None:
    first = _item(conn, "iiiiiiiiiii")
    second = _item(conn, "jjjjjjjjjjj")
    llm = FakeLLM(error)
    report = analyze_items(conn, settings, llm)
    assert report.stopped == str(error)
    assert len(llm.calls) == 1
    for item_id in (first, second):
        row = _row(conn, item_id)
        assert (row["status"], row["attempts"]) == ("enriched", 0)


def test_failed_analysis_is_retried_until_max(conn: sqlite3.Connection, settings: Settings) -> None:
    item_id = _item(conn, "kkkkkkkkkkk")
    llm = FakeLLM(LLMError("Claude API 409"))
    for _ in range(3):
        analyze_items(conn, settings, llm)
    assert _row(conn, item_id)["attempts"] == 3
    assert analyze_items(conn, settings, llm).failed == 0  # 더 시도하지 않음
    llm.responses = [GOOD]
    assert analyze_items(conn, settings, llm).analyzed == 0


def test_enrich_stage_items_are_not_candidates(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    insert_item(conn, "lllllllllll")  # imported, transcripts 없음
    insert_item(conn, "mmmmmmmmmmm", status="failed", attempts=1)  # 보강 단계 실패
    assert db.analyze_candidates(conn, max_attempts=3, limit=None) == []


# --- dry-run ---


def test_dry_run_counts_sample_and_estimates_rest(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("reelbox.ai.analyze.SAMPLE_FOR_ESTIMATE", 2)
    for i in range(5):
        _item(conn, f"nnnnnnnnn{i:02d}")
    llm = FakeLLM(GOOD)
    report = dry_run(conn, settings, llm)

    assert (report.items, report.counted) == (5, 2)
    per_item = 1000 + len(llm.calls[0])
    assert report.input_tokens == per_item * 5
    assert report.output_tokens == 600 * 5
    expected = (report.input_tokens * 0.10 + report.output_tokens * 0.50) / 1_000_000
    assert report.cost_usd == pytest.approx(expected)
    assert db.status_counts(conn)["enriched"] == 5  # 아무것도 바꾸지 않음


def test_dry_run_with_nothing(conn: sqlite3.Connection, settings: Settings) -> None:
    report = dry_run(conn, settings, FakeLLM(GOOD))
    assert (report.items, report.input_tokens, report.cost_usd) == (0, 0, 0.0)
