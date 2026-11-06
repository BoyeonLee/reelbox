"""항목별 한국어 요약·핵심 포인트·태그·raw_topic 생성.

enriched 항목의 제목·작성자·설명·자막(앞부분)을 LLM 에 보내 AnalysisOutput 모양으로 받고,
analyses 에 저장한 뒤 analyzed 로 넘긴다. 프롬프트는 prompts/<버전>.md 파일이다.
"""

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from reelbox import db
from reelbox.ai.llm import (
    LLM,
    LLMError,
    LLMFatalError,
    LLMOutputError,
    LLMRefusal,
    LLMUnavailableError,
    Usage,
)
from reelbox.config import Settings

logger = logging.getLogger(__name__)

SAMPLE_FOR_ESTIMATE = 30  # --dry-run 에서 토큰을 실제로 세어 볼 항목 수 (나머지는 평균으로 추정)
# --dry-run 의 출력 토큰 추정치 (JSON 결과 + 낮은 effort 의 생각 토큰). 실제 실행 결과로 고친다.
EST_OUTPUT_TOKENS = 600


class AnalysisOutput(BaseModel):
    """모델이 돌려줄 JSON. 프롬프트는 개수를 2~5 / 3~8 로 부탁하고, 검증은 조금 느슨하게 한다."""

    summary_ko: str = Field(min_length=1, description="2~3문장 한국어 요약")
    key_points: list[str] = Field(min_length=1, max_length=8, description="핵심 2~5개")
    raw_topic: str = Field(min_length=1, max_length=40, description="2~4단어 주제 이름")
    tags: list[str] = Field(min_length=1, max_length=12, description="검색용 키워드 3~8개")
    content_language: str = Field(min_length=2, max_length=8, description="ISO 639-1 코드")

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, tags: list[str]) -> list[str]:
        """'#파스타' → '파스타', 빈 값·중복 제거 (순서 유지)."""
        cleaned = [t.strip().lstrip("#").strip() for t in tags]
        return list(dict.fromkeys(t for t in cleaned if t))

    @field_validator("key_points")
    @classmethod
    def _clean_points(cls, points: list[str]) -> list[str]:
        return [p.strip() for p in points if p.strip()]


class Prompt(BaseModel):
    version: str
    system: str


def load_prompt(prompts_dir: Path, name: str) -> Prompt:
    """prompts/<name>.md 를 읽는다. 맨 위 '---' 사이 머리말은 기록용이라 모델에 보내지 않는다."""
    text = (prompts_dir / f"{name}.md").read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    if text.startswith("---\n"):
        header, _, text = text[4:].partition("\n---\n")
        for line in header.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip()
    return Prompt(version=meta.get("version", name), system=text.strip())


_PLATFORM_KO = {"instagram": "인스타그램", "youtube": "유튜브"}
_SOURCE_KO = {"captions": "자막", "whisper": "음성 받아쓰기"}


def build_user_message(item: db.AnalyzeInput, transcript_max_chars: int) -> str:
    """모델에 보낼 항목 내용. 자막은 앞부분만 (DESIGN 5-3: 약 6,000자)."""
    lines = [
        "<item>",
        f"플랫폼: {_PLATFORM_KO.get(item.platform, item.platform)}",
        f"종류: {'사진 게시물' if item.media_type == 'image' else '영상'}",
    ]
    if item.title:
        lines.append(f"제목: {item.title}")
    if item.author:
        lines.append(f"작성자: {item.author}")
    if item.collection:
        lines.append(f"사용자가 넣은 컬렉션: {item.collection}")
    lines.append(f"설명:\n{item.description.strip() if item.description else '(없음)'}")
    if item.transcript_text:
        text = item.transcript_text.strip()
        if len(text) > transcript_max_chars:
            text = text[:transcript_max_chars] + "\n…(뒷부분 생략)"
        source = _SOURCE_KO.get(item.transcript_source or "", "자막")
        lines.append(f"{source}:\n{text}")
    else:
        lines.append("자막: (없음)")
    lines.append("</item>")
    return "\n".join(lines)


class AnalyzeReport(BaseModel):
    analyzed: int = 0
    failed: int = 0
    retried: int = 0  # 형식 오류로 한 번 더 부른 횟수
    stopped: str | None = None  # 전체를 멈춘 이유
    usage: Usage = Usage()


class DryRunReport(BaseModel):
    items: int
    counted: int  # 실제로 토큰을 센 항목 수
    input_tokens: int  # 추정 포함 전체 입력 토큰
    output_tokens: int  # 추정
    cost_usd: float
    model: str
    prompt_version: str


def estimate_cost(settings: Settings, usage: Usage) -> float:
    return (
        usage.input_tokens * settings.analyze_price_input_per_mtok
        + usage.output_tokens * settings.analyze_price_output_per_mtok
    ) / 1_000_000


def _analyze_one(
    llm: LLM, prompt: Prompt, user: str, report: AnalyzeReport
) -> tuple[AnalysisOutput, Usage]:
    """형식 오류면 한 번만 다시 부른다. 거절·기타 오류는 그대로 올린다."""
    try:
        result = llm.structured(system=prompt.system, user=user, schema=AnalysisOutput)
    except LLMOutputError as exc:
        logger.info("형식 오류라 한 번 더 시도: %s", exc)
        report.retried += 1
        result = llm.structured(system=prompt.system, user=user, schema=AnalysisOutput)
    return result.output, result.usage


def analyze_items(
    conn: sqlite3.Connection,
    settings: Settings,
    llm: LLM,
    *,
    limit: int | None = None,
    on_item: Callable[[int, int, db.AnalyzeInput], None] | None = None,
) -> AnalyzeReport:
    """분석할 차례인 항목을 처리한다. 이미 analyzed 인 것은 건드리지 않는다(멱등)."""
    prompt = load_prompt(settings.prompts_dir, settings.analyze_prompt)
    items = db.analyze_candidates(conn, max_attempts=settings.max_attempts, limit=limit)
    report = AnalyzeReport()
    for n, item in enumerate(items, start=1):
        if on_item is not None:
            on_item(n, len(items), item)
        user = build_user_message(item, settings.transcript_max_chars)
        try:
            output, usage = _analyze_one(llm, prompt, user, report)
        except (LLMFatalError, LLMUnavailableError) as exc:
            # 키·잔액 문제나 속도 제한은 다음 항목도 똑같이 실패한다 → 바로 멈춤.
            # 항목 잘못이 아니라서 실패로 기록하지 않는다 (enriched 그대로, 다음에 이어서).
            report.stopped = str(exc)
            break
        except LLMError as exc:
            kind = "거절" if isinstance(exc, LLMRefusal) else "오류"
            logger.warning("분석 실패 (%s, %s): %s", kind, item.url, exc)
            db.mark_failed(conn, item.id, str(exc))
            report.failed += 1
            continue
        report.usage = report.usage + usage
        db.save_analysis(
            conn,
            item.id,
            db.AnalysisRecord(
                summary_ko=output.summary_ko,
                key_points=output.key_points,
                raw_topic=output.raw_topic,
                tags=output.tags,
                content_language=output.content_language,
                model=llm.model,
                prompt_version=prompt.version,
            ),
        )
        report.analyzed += 1
    return report


def dry_run(
    conn: sqlite3.Connection, settings: Settings, llm: LLM, *, limit: int | None = None
) -> DryRunReport:
    """모델을 부르지 않고 항목 수·예상 토큰·비용을 계산한다. 토큰은 공식 API 로 센다(무료)."""
    prompt = load_prompt(settings.prompts_dir, settings.analyze_prompt)
    items = db.analyze_candidates(conn, max_attempts=settings.max_attempts, limit=limit)
    sample = items[:SAMPLE_FOR_ESTIMATE]
    counted = [
        llm.count_tokens(
            system=prompt.system,
            user=build_user_message(item, settings.transcript_max_chars),
            schema=AnalysisOutput,
        )
        for item in sample
    ]
    if counted:
        average = sum(counted) / len(counted)
        input_tokens = sum(counted) + round(average * (len(items) - len(sample)))
    else:
        input_tokens = 0
    usage = Usage(input_tokens=input_tokens, output_tokens=EST_OUTPUT_TOKENS * len(items))
    return DryRunReport(
        items=len(items),
        counted=len(counted),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=estimate_cost(settings, usage),
        model=llm.model,
        prompt_version=prompt.version,
    )
