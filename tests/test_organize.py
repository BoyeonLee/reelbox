"""주제 정리(organize). LLM 과 임베딩은 가짜로 바꾼다."""

import re
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

from reelbox import db
from reelbox.ai import organize as organizer
from reelbox.ai.llm import LLMError, LLMFatalError, LLMOutputError, StructuredResult, Usage
from reelbox.ai.organize import (
    AssignBatch,
    Assignment,
    MappingBatch,
    RawTopicMapping,
    TopicDraft,
    TopicPlan,
)
from reelbox.config import Settings

from .conftest import FakeEmbedder, analyzed_item, set_user_meta

Handler = Callable[[str], Any]


class SchemaLLM:
    """schema 마다 응답을 만드는 함수를 둔다. 함수가 예외 객체를 돌려주면 raise."""

    model = "fake-model"

    def __init__(self, handlers: dict[type[BaseModel], Handler]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, str]] = []

    def structured(self, *, system: str, user: str, schema: type[BaseModel]) -> StructuredResult:
        self.calls.append((schema.__name__, user))
        value = self.handlers[schema](user)
        if isinstance(value, Exception):
            raise value
        return StructuredResult(output=value, usage=Usage(input_tokens=100, output_tokens=10))

    def count_tokens(self, **kwargs: object) -> int:
        return 0

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


PLAN = TopicPlan(
    topics=[
        TopicDraft(name="요리", description="음식 만드는 법"),
        TopicDraft(name="뷰티", description="화장·화장품"),
    ]
)

# raw_topic 이름에 이 낱말이 있으면 그 이름의 주제로 대응시킨다
KEYWORDS = {"레시피": "요리", "요리": "요리", "메이크업": "뷰티", "브러시": "뷰티"}


def map_by_keyword(user: str) -> MappingBatch:
    """<topics> 의 번호를 읽어 raw_topic 을 낱말로 대응시킨다. 모르는 건 대응표에서 뺀다."""
    numbers = {name: int(n) for n, name in re.findall(r"^(\d+)\. ([^:]+):", user, re.M)}
    raw = user.split("<raw_topics>")[1]
    mappings = []
    for name in re.findall(r"^- (.+)$", raw, re.M):
        for word, topic in KEYWORDS.items():
            if word in name and topic in numbers:
                mappings.append(RawTopicMapping(raw_topic=name, topic=numbers[topic]))
                break
    return MappingBatch(mappings=mappings)


def pick_first_candidate(user: str) -> AssignBatch:
    picks = []
    for block in re.findall(r'<item number="(\d+)">(.*?)</item>', user, re.S):
        first = re.search(r"^- (\d+)\. ", block[1], re.M)
        picks.append(Assignment(item=int(block[0]), topic=int(first.group(1)) if first else 0))
    return AssignBatch(assignments=picks)


def default_llm(**overrides: Handler) -> SchemaLLM:
    handlers: dict[type[BaseModel], Handler] = {
        TopicPlan: lambda user: PLAN,
        MappingBatch: map_by_keyword,
        AssignBatch: pick_first_candidate,
    }
    for name, fn in overrides.items():
        handlers[{"plan": TopicPlan, "map": MappingBatch, "assign": AssignBatch}[name]] = fn
    return SchemaLLM(handlers)


def primary_topics(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute(
        """
        SELECT it.item_id, t.name FROM item_topics AS it JOIN topics AS t ON t.id = it.topic_id
        WHERE it.is_primary = 1
        """
    )
    return {r["item_id"]: r["name"] for r in rows}


def run(
    conn: sqlite3.Connection,
    settings: Settings,
    llm: SchemaLLM,
    embedder: FakeEmbedder | None = None,
    **kwargs: Any,
) -> organizer.OrganizeReport:
    return organizer.organize(conn, settings, llm, embedder or FakeEmbedder(), **kwargs)


# --- 전체 정리 ---


def test_first_run_does_full_organize(conn: sqlite3.Connection, settings: Settings) -> None:
    a = analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피")
    b = analyzed_item(conn, "bbbbbbbbbbb", raw_topic="파스타 레시피")
    c = analyzed_item(conn, "ccccccccccc", raw_topic="아이메이크업", status="indexed")
    llm = default_llm()
    embedder = FakeEmbedder()

    report = run(conn, settings, llm, embedder)

    assert report.mode == "full"
    assert report.stopped is None
    assert (report.topics_created, report.mapped, report.assigned) == (2, 3, 3)
    assert primary_topics(conn) == {a: "요리", b: "요리", c: "뷰티"}
    assert llm.names() == ["TopicPlan", "MappingBatch"]
    assert embedder.calls == []  # 대응표로 다 끝나면 임베딩도 필요 없다
    plan_msg = llm.calls[0][1]
    assert "- 파스타 레시피 (2개)" in plan_msg
    assert "<fixed_topics>\n(없음)" in plan_msg
    assert {t.name: t.items for t in db.topic_counts(conn)} == {"요리": 2, "뷰티": 1}


def test_full_keeps_user_edited_topics_and_replaces_auto_ones(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    conn.execute(
        "INSERT INTO topics (name, description, is_user_edited) VALUES ('요리', '내 것', 1)"
    )
    conn.execute("INSERT INTO topics (name) VALUES ('옛 자동 주제')")
    conn.commit()
    a = analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피")
    b = analyzed_item(conn, "bbbbbbbbbbb", raw_topic="브러시 추천")
    llm = default_llm()

    report = run(conn, settings, llm, full=True)

    topics = {t.name: t for t in db.list_topics(conn)}
    assert set(topics) == {"요리", "뷰티"}  # 옛 자동 주제는 지워지고, 같은 이름 '요리' 는 안 만듦
    assert topics["요리"].is_user_edited and topics["요리"].description == "내 것"
    assert report.topics_created == 1
    assert primary_topics(conn) == {a: "요리", b: "뷰티"}
    plan_msg, map_msg = llm.calls[0][1], llm.calls[1][1]
    assert "- 요리: 내 것" in plan_msg
    assert map_msg.index("1. 요리: 내 것") < map_msg.index("2. 뷰티")  # 고정 주제가 먼저


def test_full_reassigns_everything(conn: sqlite3.Connection, settings: Settings) -> None:
    a = analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피")
    run(conn, settings, default_llm())
    plan2 = TopicPlan(topics=[TopicDraft(name="음식", description="먹는 것")])
    llm = default_llm(
        plan=lambda u: plan2,
        map=lambda u: MappingBatch(mappings=[RawTopicMapping(raw_topic="파스타 레시피", topic=1)]),
    )
    run(conn, settings, llm, full=True)
    assert primary_topics(conn) == {a: "음식"}
    assert [t.name for t in db.list_topics(conn)] == ["음식"]


def test_full_maps_in_batches(conn: sqlite3.Connection, settings: Settings) -> None:
    settings.organize_map_batch = 2
    for n, sid in enumerate(("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc")):
        analyzed_item(conn, sid, raw_topic=f"레시피 {n}")
    llm = default_llm()
    report = run(conn, settings, llm)
    assert llm.names() == ["TopicPlan", "MappingBatch", "MappingBatch"]
    assert report.mapped == 3


def test_full_assigns_unmapped_raw_topics_with_candidates(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    a = analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피")
    b = analyzed_item(conn, "bbbbbbbbbbb", raw_topic="알 수 없는 것", title="뭔가")
    llm = default_llm()
    embedder = FakeEmbedder()

    report = run(conn, settings, llm, embedder)

    assert (report.mapped, report.by_llm, report.embedded) == (1, 1, 1)
    assert set(primary_topics(conn)) == {a, b}
    assert llm.names()[-1] == "AssignBatch"
    assert db.load_vectors(conn, [b])  # 계산한 임베딩은 저장해서 index 가 다시 쓴다


def test_full_ignores_bad_mappings(conn: sqlite3.Connection, settings: Settings) -> None:
    analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피")
    bad = MappingBatch(
        mappings=[
            RawTopicMapping(raw_topic="파스타 레시피", topic=99),  # 없는 번호
            RawTopicMapping(raw_topic="지어낸 주제", topic=1),  # 보내지 않은 raw_topic
        ]
    )
    report = run(conn, settings, default_llm(map=lambda u: bad))
    assert report.mapped == 0
    assert report.by_llm == 1  # 임베딩 후보로 마저 배정


def test_full_failure_writes_nothing(conn: sqlite3.Connection, settings: Settings) -> None:
    conn.execute("INSERT INTO topics (name) VALUES ('기존')")
    conn.commit()
    a = analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피")
    db.assign_topics(conn, {a: 1})
    llm = default_llm(map=lambda u: LLMError("Claude API 409: 이상함"))

    report = run(conn, settings, llm, full=True)

    assert report.stopped and "409" in report.stopped
    assert [t.name for t in db.list_topics(conn)] == ["기존"]
    assert primary_topics(conn) == {a: "기존"}


def test_full_retries_once_on_output_error(conn: sqlite3.Connection, settings: Settings) -> None:
    analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피")
    answers: list[Any] = [LLMOutputError("JSON 이상"), PLAN]
    report = run(conn, settings, default_llm(plan=lambda u: answers.pop(0)))
    assert report.retried == 1
    assert report.stopped is None


def test_full_skips_hidden_and_deleted(conn: sqlite3.Connection, settings: Settings) -> None:
    shown = analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피")
    hidden = analyzed_item(conn, "bbbbbbbbbbb", raw_topic="숨긴 레시피")
    trashed = analyzed_item(conn, "ccccccccccc", raw_topic="버린 레시피")
    set_user_meta(conn, hidden, hidden_at="2026-11-01T00:00:00Z")
    set_user_meta(conn, trashed, deleted_at="2026-11-01T00:00:00Z")
    llm = default_llm()
    run(conn, settings, llm)
    assert "숨긴" not in llm.calls[0][1] and "버린" not in llm.calls[0][1]
    assert primary_topics(conn) == {shown: "요리"}


def test_nothing_to_organize(conn: sqlite3.Connection, settings: Settings) -> None:
    analyzed_item(conn, "aaaaaaaaaaa", status="enriched")
    llm = default_llm()
    report = run(conn, settings, llm)
    assert llm.calls == []
    assert report.assigned == 0


# --- 새 항목 배정 ---


@pytest.fixture
def organized(conn: sqlite3.Connection, settings: Settings) -> sqlite3.Connection:
    analyzed_item(conn, "aaaaaaaaaaa", raw_topic="파스타 레시피", title="파스타")
    analyzed_item(conn, "bbbbbbbbbbb", raw_topic="아이메이크업", title="눈화장")
    run(conn, settings, default_llm())
    return conn


def test_new_item_with_known_raw_topic_reuses_topic_without_llm(
    organized: sqlite3.Connection, settings: Settings
) -> None:
    new = analyzed_item(organized, "ccccccccccc", raw_topic="파스타 레시피")
    llm = default_llm()
    embedder = FakeEmbedder()
    report = run(organized, settings, llm, embedder)
    assert report.mode == "incremental"
    assert report.reused == 1
    assert llm.calls == [] and embedder.calls == []
    assert primary_topics(organized)[new] == "요리"


def test_new_item_gets_three_nearest_candidates(
    organized: sqlite3.Connection, settings: Settings
) -> None:
    organized.execute("INSERT INTO topics (name, description) VALUES ('여행', '여행지 소개')")
    organized.execute("INSERT INTO topics (name, description) VALUES ('운동', '헬스·요가')")
    organized.commit()
    new = analyzed_item(
        organized, "ccccccccccc", raw_topic="크림 파스타", title="크림 파스타", summary="요리"
    )
    llm = default_llm()
    report = run(organized, settings, llm)

    assert report.by_llm == 1
    assert llm.names() == ["AssignBatch"]
    message = llm.calls[0][1]
    assert message.count("\n- ") == 3  # 주제 4개 중 후보 3개
    assert "raw_topic: 크림 파스타" in message
    first_candidate = re.search(r"^- \d+\. ([^:]+):", message, re.M)
    assert first_candidate and first_candidate.group(1) == "요리"
    assert primary_topics(organized)[new] == "요리"


def test_answer_zero_falls_back_to_nearest_and_counts_no_fit(
    organized: sqlite3.Connection, settings: Settings
) -> None:
    new = analyzed_item(organized, "ccccccccccc", raw_topic="파스타 만들기", title="파스타")
    zero = AssignBatch(assignments=[Assignment(item=1, topic=0)])
    report = run(organized, settings, default_llm(assign=lambda u: zero))
    assert (report.by_llm, report.no_fit) == (0, 1)
    assert primary_topics(organized)[new] == "요리"


def test_missing_or_invalid_answers_fall_back(
    organized: sqlite3.Connection, settings: Settings
) -> None:
    analyzed_item(organized, "ccccccccccc", raw_topic="새 주제 하나")
    analyzed_item(organized, "ddddddddddd", raw_topic="새 주제 둘")
    answer = AssignBatch(assignments=[Assignment(item=1, topic=999)])  # 2번은 답이 없음
    report = run(organized, settings, default_llm(assign=lambda u: answer))
    assert report.no_fit == 2
    assert len(primary_topics(organized)) == 4


def test_assign_batches_and_limit(organized: sqlite3.Connection, settings: Settings) -> None:
    settings.organize_assign_batch = 2
    for sid in ("ccccccccccc", "ddddddddddd", "eeeeeeeeeee", "fffffffffff"):
        analyzed_item(organized, sid, raw_topic=f"새 {sid}")
    llm = default_llm()
    report = run(organized, settings, llm, limit=3)
    assert llm.names() == ["AssignBatch", "AssignBatch"]
    assert report.by_llm == 3
    assert len(db.unassigned_items(organized, limit=None)) == 1


def test_assign_error_leaves_batch_for_next_run(
    organized: sqlite3.Connection, settings: Settings
) -> None:
    new = analyzed_item(organized, "ccccccccccc", raw_topic="새것")
    report = run(organized, settings, default_llm(assign=lambda u: LLMError("이상함")))
    assert report.left == 1 and report.stopped is None
    assert new not in primary_topics(organized)
    again = run(organized, settings, default_llm())
    assert again.by_llm == 1


def test_fatal_error_stops(organized: sqlite3.Connection, settings: Settings) -> None:
    analyzed_item(organized, "ccccccccccc", raw_topic="새것")
    llm = default_llm(assign=lambda u: LLMFatalError("Claude API 401"))
    report = run(organized, settings, llm)
    assert report.stopped == "Claude API 401"
    assert report.left == 1


def test_hidden_new_items_are_not_assigned(
    organized: sqlite3.Connection, settings: Settings
) -> None:
    hidden = analyzed_item(organized, "ccccccccccc", raw_topic="새것")
    set_user_meta(organized, hidden, hidden_at="2026-11-01T00:00:00Z")
    llm = default_llm()
    run(organized, settings, llm)
    assert llm.calls == []


def test_raw_topic_assignments_prefers_majority(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO topics (name) VALUES ('가'), ('나')")
    conn.commit()
    ids = [analyzed_item(conn, f"{c * 11}", raw_topic="같은 것") for c in "abc"]
    db.assign_topics(conn, {ids[0]: 1, ids[1]: 2, ids[2]: 2})
    assert db.raw_topic_assignments(conn) == {"같은 것": 2}
