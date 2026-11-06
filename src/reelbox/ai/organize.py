"""raw_topic 들을 대주제 10~20개로 정리한다 (DESIGN.md 5-4).

비유: 직원들이 메모에 제멋대로 붙인 라벨(raw_topic)을 팀장이 모아 보고 바인더로 정리하는 것.

- 전체 정리(--full, 또는 주제가 하나도 없을 때)
  1. raw_topic 과 개수를 보내 대주제 목록을 받는다 (내가 고친 주제는 고정값으로 같이 보냄).
  2. raw_topic 을 organize_map_batch 개씩 보내 "raw_topic → 주제 번호" 대응표를 받는다.
     한 번에 다 받으면 출력이 너무 길어 잘릴 수 있어서 나눈다.
  3. 자동 주제는 지우고 새로 만든 뒤 대응표대로 항목을 잇는다 (한 트랜잭션).
  4. 대응표에서 빠진 항목은 아래 "새 항목 배정"으로 마저 처리한다.
- 새 항목 배정(기본): 주제가 없는 항목만.
  - 같은 raw_topic 의 항목이 이미 어떤 주제에 있으면 그 주제를 그대로 쓴다 (LLM 안 부름).
  - 아니면 임베딩이 가장 가까운 주제 후보 3개를 뽑아 Claude 가 고른다.
"""

import logging
import sqlite3
from collections.abc import Callable
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field, ValidationError

from reelbox import db
from reelbox.ai.analyze import Prompt, load_prompt
from reelbox.ai.llm import (
    LLM,
    LLMError,
    LLMFatalError,
    LLMOutputError,
    LLMUnavailableError,
    Usage,
)
from reelbox.config import Settings
from reelbox.search.embed import Embedder
from reelbox.search.index import ensure_vectors

logger = logging.getLogger(__name__)

CANDIDATES = 3  # 새 항목마다 Claude 에게 보여줄 후보 주제 수
TOPIC_SAMPLE_RAW = 15  # 주제 임베딩 텍스트에 넣을, 그 주제의 raw_topic 수

StatusFn = Callable[[str], None]


# --- 모델이 돌려줄 JSON 모양 ---


class TopicDraft(BaseModel):
    name: str = Field(min_length=1, max_length=20, description="2~8글자 한국어 주제 이름")
    description: str = Field(min_length=1, description="이 주제에 들어갈 영상 한 문장 설명")


class TopicPlan(BaseModel):
    topics: list[TopicDraft] = Field(min_length=1, max_length=25)


class RawTopicMapping(BaseModel):
    raw_topic: str
    topic: int = Field(description="<topics> 의 주제 번호")


class MappingBatch(BaseModel):
    mappings: list[RawTopicMapping]


class Assignment(BaseModel):
    item: int = Field(description="<items> 의 영상 번호")
    topic: int = Field(description="후보 주제 번호, 어울리는 게 없으면 0")


class AssignBatch(BaseModel):
    assignments: list[Assignment]


# --- 결과 ---


class OrganizeReport(BaseModel):
    mode: Literal["full", "incremental"]
    topics_created: int = 0
    mapped: int = 0  # 전체 정리의 대응표로 주제를 받은 항목 수
    reused: int = 0  # 같은 raw_topic 의 기존 주제를 그대로 쓴 수 (LLM 안 부름)
    by_llm: int = 0  # 후보 중에서 Claude 가 골라 준 수
    no_fit: int = 0  # Claude 가 "어울리는 후보 없음"이라 해서 가장 가까운 후보로 넣은 수
    left: int = 0  # 이번에 주제를 못 받은 항목 수 (다음 실행에 다시 시도)
    embedded: int = 0  # 이번에 새로 계산한 임베딩 수
    retried: int = 0  # 형식 오류로 한 번 더 부른 횟수
    usage: Usage = Usage()
    stopped: str | None = None  # 전체를 멈춘 이유

    @property
    def assigned(self) -> int:
        return self.mapped + self.reused + self.by_llm + self.no_fit


def _call[T: BaseModel](
    llm: LLM, prompt: Prompt, user: str, schema: type[T], report: OrganizeReport
) -> T:
    """형식 오류면 한 번만 다시 부른다. (def f[T: X] 는 3.12 의 제네릭 함수 문법)"""
    try:
        result = llm.structured(system=prompt.system, user=user, schema=schema)
    except LLMOutputError as exc:
        logger.info("형식 오류라 한 번 더 시도: %s", exc)
        report.retried += 1
        result = llm.structured(system=prompt.system, user=user, schema=schema)
    report.usage = report.usage + result.usage
    return result.output


def _chunks[T](values: list[T], size: int) -> list[list[T]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


# --- 전체 정리 ---


def build_topics_message(raw: list[tuple[str, int]], fixed: list[db.Topic]) -> str:
    lines = ["<task>topics</task>", "<fixed_topics>"]
    lines += [
        f"- {t.name}: {t.description}" if t.description else f"- {t.name}" for t in fixed
    ] or ["(없음)"]
    lines += ["</fixed_topics>", f'<raw_topics count="{len(raw)}">']
    lines += [f"- {name} ({n}개)" for name, n in raw]
    lines.append("</raw_topics>")
    return "\n".join(lines)


def build_map_message(numbered: list[tuple[str, str | None]], raw_names: list[str]) -> str:
    lines = ["<task>map</task>", "<topics>"]
    lines += [f"{n}. {name}: {desc or ''}" for n, (name, desc) in enumerate(numbered, start=1)]
    lines += ["</topics>", "<raw_topics>"]
    lines += [f"- {name}" for name in raw_names]
    lines.append("</raw_topics>")
    return "\n".join(lines)


def _clean_drafts(drafts: list[TopicDraft], fixed: list[db.Topic]) -> list[db.NewTopic]:
    """이름 앞뒤 공백 정리, 고정 주제·서로 이름이 겹치는 것 빼기."""
    seen = {t.name for t in fixed}
    result: list[db.NewTopic] = []
    for d in drafts:
        name = d.name.strip()
        if name and name not in seen:
            seen.add(name)
            result.append(db.NewTopic(name=name, description=d.description.strip()))
    return result


def _full(
    conn: sqlite3.Connection,
    settings: Settings,
    llm: LLM,
    report: OrganizeReport,
    on_status: StatusFn,
) -> None:
    """대주제를 새로 짜고 raw_topic 대응표대로 항목을 잇는다. LLM 이 다 끝나야 DB 에 쓴다."""
    prompt = load_prompt(settings.prompts_dir, settings.organize_prompt)
    raw = db.raw_topic_counts(conn)
    if not raw:
        return
    fixed = [t for t in db.list_topics(conn) if t.is_user_edited]

    on_status(f"대주제 만드는 중 (raw_topic {len(raw)}종류)")
    plan = _call(llm, prompt, build_topics_message(raw, fixed), TopicPlan, report)
    new_topics = _clean_drafts(plan.topics, fixed)

    # 대응표용 번호: 고정 주제 먼저, 그다음 새 주제. 값은 replace_topics 가 받는 모양
    # (고정 주제는 id, 새 주제는 이름).
    targets: list[int | str] = [t.id for t in fixed] + [t.name for t in new_topics]
    numbered = [(t.name, t.description) for t in fixed] + [
        (t.name, t.description) for t in new_topics
    ]
    raw_names = [name for name, _ in raw]
    mapping: dict[str, int | str] = {}
    batches = _chunks(raw_names, settings.organize_map_batch)
    for n, batch in enumerate(batches, start=1):
        on_status(f"raw_topic 대응표 받는 중 {n}/{len(batches)}")
        result = _call(llm, prompt, build_map_message(numbered, batch), MappingBatch, report)
        wanted = set(batch)
        for m in result.mappings:
            if m.raw_topic in wanted and 1 <= m.topic <= len(targets):
                mapping[m.raw_topic] = targets[m.topic - 1]
    missing = len(raw_names) - len(mapping)
    if missing:
        logger.warning("대응표에서 빠진 raw_topic %d개는 임베딩 후보로 배정한다", missing)

    db.replace_topics(conn, new_topics, mapping)
    report.topics_created = len(new_topics)
    counts = dict(raw)
    report.mapped = sum(counts[name] for name in mapping)


# --- 새 항목 배정 ---


def _topic_text(topic: db.Topic, raw_topics: list[str]) -> str:
    parts = [topic.name, topic.description or ""]
    if raw_topics:
        parts.append("예: " + ", ".join(raw_topics))
    return "\n".join(p for p in parts if p)


def build_assign_message(batch: list[db.EmbedInput], candidates: list[list[db.Topic]]) -> str:
    lines = ["<items>"]
    for n, (item, cands) in enumerate(zip(batch, candidates, strict=True), start=1):
        lines.append(f'<item number="{n}">')
        if item.title:
            lines.append(f"제목: {item.title}")
        lines.append(f"요약: {item.summary_ko or '(없음)'}")
        lines.append(f"raw_topic: {item.raw_topic or '(없음)'}")
        lines.append("후보:")
        lines += [f"- {t.id}. {t.name}: {t.description or ''}" for t in cands]
        lines.append("</item>")
    lines.append("</items>")
    return "\n".join(lines)


def _assign_new(
    conn: sqlite3.Connection,
    settings: Settings,
    llm: LLM,
    embedder: Embedder,
    report: OrganizeReport,
    on_status: StatusFn,
    limit: int | None,
) -> None:
    items = db.unassigned_items(conn, limit=limit)
    topics = db.list_topics(conn)
    if not items:
        return
    if not topics:
        report.left += len(items)
        return

    # 1) 같은 raw_topic 이 이미 들어간 주제가 있으면 그대로 쓴다
    reuse = db.raw_topic_assignments(conn)
    direct = {it.id: reuse[it.raw_topic] for it in items if it.raw_topic in reuse}
    db.assign_topics(conn, direct)
    report.reused += len(direct)
    rest = [it for it in items if it.id not in direct]
    if not rest:
        return

    # 2) 임베딩으로 후보 3개 → Claude 가 고른다
    on_status(f"새 항목 {len(rest)}개 임베딩 계산 중")
    vectors, embedded = ensure_vectors(
        conn,
        settings,
        embedder,
        rest,
        lambda done, total: on_status(f"임베딩 계산 중 {done}/{total}"),
    )
    report.embedded += embedded
    members = db.topic_raw_topics(conn, TOPIC_SAMPLE_RAW)
    topic_matrix = embedder.encode([_topic_text(t, members.get(t.id, [])) for t in topics])
    candidates: list[list[db.Topic]] = []
    for item in rest:
        sims = topic_matrix @ np.asarray(vectors[item.id], dtype=np.float32)
        best = np.argsort(-sims)[: min(CANDIDATES, len(topics))]
        candidates.append([topics[j] for j in best])

    prompt = load_prompt(settings.prompts_dir, settings.organize_assign_prompt)
    batches = _chunks(list(range(len(rest))), settings.organize_assign_batch)
    for n, idxs in enumerate(batches, start=1):
        on_status(f"새 항목 주제 고르는 중 {n}/{len(batches)}")
        batch = [rest[i] for i in idxs]
        cands = [candidates[i] for i in idxs]
        try:
            result = _call(llm, prompt, build_assign_message(batch, cands), AssignBatch, report)
        except (LLMFatalError, LLMUnavailableError):
            report.left += sum(len(b) for b in batches[n - 1 :])
            raise
        except LLMError as exc:
            # 이 묶음만 건너뛴다. 주제가 없는 채로 남아 다음 실행에 다시 시도된다
            logger.warning("주제 배정 실패 (%d개 건너뜀): %s", len(batch), exc)
            report.left += len(batch)
            continue
        picked = {a.item: a.topic for a in result.assignments}
        chosen: dict[int, int] = {}
        for pos, (item, cand) in enumerate(zip(batch, cands, strict=True), start=1):
            answer = picked.get(pos)
            if answer in {t.id for t in cand}:
                chosen[item.id] = answer  # type: ignore[assignment]
                report.by_llm += 1
            else:
                # 0(어울리는 후보 없음)·잘못된 번호·빠진 답 → 가장 가까운 후보.
                # 주제 없이 두면 실행할 때마다 다시 LLM 을 부르게 돼서 일단 넣어 둔다.
                chosen[item.id] = cand[0].id
                report.no_fit += 1
        db.assign_topics(conn, chosen)


# --- 진입점 ---


def organize(
    conn: sqlite3.Connection,
    settings: Settings,
    llm: LLM,
    embedder: Embedder,
    *,
    full: bool = False,
    limit: int | None = None,
    on_status: StatusFn | None = None,
) -> OrganizeReport:
    """주제 정리. 주제가 하나도 없으면 --full 이 아니어도 전체 정리를 한다.

    limit 은 새 항목 배정에만 쓴다 (전체 정리는 모든 raw_topic 을 한 번에 봐야 해서).
    """
    status = on_status or (lambda _msg: None)
    run_full = full or not db.list_topics(conn)
    report = OrganizeReport(mode="full" if run_full else "incremental")
    mode = report.mode
    try:
        if mode == "full":
            _full(conn, settings, llm, report, status)
            limit = None  # 대응표에서 빠진 것까지 마저 배정
        _assign_new(conn, settings, llm, embedder, report, status, limit)
    except (LLMFatalError, LLMUnavailableError) as exc:
        report.stopped = str(exc)
    except (LLMError, ValidationError) as exc:
        # 전체 정리 중의 오류: DB 에 아직 아무것도 쓰지 않았다
        report.stopped = f"주제 정리 실패: {exc}"
    return report
