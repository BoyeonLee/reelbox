"""reelbox run 파이프라인 (Phase 8 결정). yt-dlp·Claude·임베딩은 전부 가짜다."""

import sqlite3

import pytest

from reelbox.ai.organize import AssignBatch
from reelbox.config import Settings
from reelbox.pipeline import PipelineDeps, run_pipeline
from reelbox.worker import SHARE_SOURCE

from .conftest import FakeEmbedder, analyzed_item
from .test_analyze import GOOD, FakeLLM
from .test_enrich_runner import SUBS, Env, yt_info
from .test_organize import SchemaLLM, pick_first_candidate


class Setup:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings
        self.env = Env(conn, settings)
        self.embedder = FakeEmbedder()
        self.embedder_loads = 0

    def load_embedder(self) -> FakeEmbedder:
        self.embedder_loads += 1
        return self.embedder

    def deps(self) -> PipelineDeps:
        return PipelineDeps(
            enrich=self.env.deps(),
            llm=FakeLLM(GOOD),
            organize_llm=SchemaLLM({AssignBatch: pick_first_candidate}),
            embedder=self.load_embedder,
        )

    def status(self, item_id: int) -> str:
        return self.env.item(item_id)["status"]

    def add_topic(self) -> None:
        old = analyzed_item(self.conn, "ooooooooooo", title="예전 파스타", raw_topic=GOOD.raw_topic)
        topic_id = self.conn.execute("INSERT INTO topics (name) VALUES ('요리')").lastrowid
        self.conn.execute(
            "INSERT INTO item_topics (item_id, topic_id, is_primary) VALUES (?, ?, 1)",
            (old, topic_id),
        )
        self.conn.commit()


@pytest.fixture
def s(conn: sqlite3.Connection, settings: Settings) -> Setup:
    return Setup(conn, settings)


def test_without_limit_leaves_export_items_alone(s: Setup) -> None:
    s.add_topic()
    shared = s.env.yt("aaaaaaaaaaa", {**yt_info("aaaaaaaaaaa"), **SUBS}, imported_from=SHARE_SOURCE)
    exported = s.env.yt("bbbbbbbbbbb", imported_from="youtube_sync")

    report = run_pipeline(s.conn, s.settings, s.deps(), limit=None)

    assert s.status(shared) == "indexed"
    assert s.status(exported) == "imported"  # 아직 안 한 내보내기 항목은 그대로
    assert report.enrich.enriched == 1
    assert report.analyze.analyzed == 1
    assert report.organize is not None and report.organize.mode == "incremental"
    assert report.index.indexed == 2  # 공유 항목 + 주제 정리 때 쓴 예전 항목


def test_limit_picks_export_items_too(s: Setup) -> None:
    s.add_topic()
    first = s.env.yt(
        "bbbbbbbbbbb", {**yt_info("bbbbbbbbbbb"), **SUBS}, imported_from="youtube_sync"
    )
    second = s.env.yt("ccccccccccc", imported_from="youtube_sync")

    report = run_pipeline(s.conn, s.settings, s.deps(), limit=1)

    assert s.status(first) == "indexed"
    assert s.status(second) == "imported"
    assert report.enrich.enriched == 1


def test_nothing_to_do_does_not_load_model(s: Setup) -> None:
    report = run_pipeline(s.conn, s.settings, s.deps(), limit=None)
    assert s.embedder_loads == 0
    assert report.organize is None
    assert report.index.indexed == 0


def test_no_topics_skips_organize_but_still_indexes(s: Setup) -> None:
    item = s.env.yt("aaaaaaaaaaa", {**yt_info("aaaaaaaaaaa"), **SUBS}, imported_from=SHARE_SOURCE)
    report = run_pipeline(s.conn, s.settings, s.deps(), limit=None)
    assert report.organize is None
    assert report.organize_skipped_no_topics is True
    assert s.status(item) == "indexed"
