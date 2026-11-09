"""공유 항목 자동 처리 워커. yt-dlp·whisper·Claude·임베딩은 전부 가짜다."""

import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from reelbox import db
from reelbox.ai.llm import LLMUnavailableError
from reelbox.ai.organize import AssignBatch
from reelbox.config import Settings
from reelbox.worker import SHARE_SOURCE, Worker, WorkerDeps
from reelbox.ytdlp import BlockedError

from .conftest import FakeEmbedder, analyzed_item
from .test_analyze import GOOD, FakeLLM
from .test_enrich_runner import NOW, SUBS, Env, ig_info, yt_info
from .test_organize import SchemaLLM, pick_first_candidate


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Setup:
    """워커 하나와 그 가짜 외부 세계."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings
        self.env = Env(conn, settings)
        self.llm = FakeLLM(GOOD)
        self.organize_llm = SchemaLLM({AssignBatch: pick_first_candidate})
        self.embedder: FakeEmbedder | None = FakeEmbedder()
        self.clock = Clock()
        self.has_llm = True

    def worker(self) -> Worker:
        deps = WorkerDeps(
            enrich=self.env.deps(),
            llm=self.llm if self.has_llm else None,
            organize_llm=self.organize_llm if self.has_llm else None,
            embedder=lambda: self.embedder,
            now=self.clock,
        )
        return Worker(self.settings, deps)

    def shared_yt(self, video_id: str, response: Any = None) -> int:
        return self.env.yt(video_id, response, imported_from=SHARE_SOURCE)

    def shared_ig(self, shortcode: str, response: Any = None) -> int:
        return self.env.ig(shortcode, response, imported_from=SHARE_SOURCE)

    def status(self, item_id: int) -> str:
        return self.env.item(item_id)["status"]

    def topic_of(self, item_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT t.name FROM item_topics AS it JOIN topics AS t ON t.id = it.topic_id"
            " WHERE it.item_id = ?",
            (item_id,),
        ).fetchone()
        return None if row is None else row[0]


@pytest.fixture
def s(conn: sqlite3.Connection, settings: Settings) -> Setup:
    setup = Setup(conn, settings)
    # 이미 정리된 보관함: 주제 "요리"에 같은 raw_topic(파스타 레시피) 항목이 있다
    old = analyzed_item(conn, "ooooooooooo", title="예전 파스타", raw_topic=GOOD.raw_topic)
    topic_id = conn.execute("INSERT INTO topics (name) VALUES ('요리')").lastrowid
    conn.execute(
        "INSERT INTO item_topics (item_id, topic_id, is_primary) VALUES (?, ?, 1)",
        (old, topic_id),
    )
    conn.commit()
    return setup


def test_shared_item_goes_all_the_way_to_indexed(s: Setup) -> None:
    item = s.shared_yt("aaaaaaaaaaa", {**yt_info("aaaaaaaaaaa"), **SUBS})
    report = s.worker().run_once()

    assert s.status(item) == "indexed"
    assert s.topic_of(item) == "요리"  # 같은 raw_topic 의 주제를 그대로 씀 (LLM 안 부름)
    assert (report.enriched, report.analyzed, report.assigned) == (1, 1, 1)
    assert report.indexed == 2  # 미리 있던 analyzed 항목도 같이 색인


def test_does_not_touch_items_from_exports(s: Setup) -> None:
    exported = s.env.yt("bbbbbbbbbbb", imported_from="instagram_export")
    s.worker().run_once()
    assert s.status(exported) == "imported"
    assert "https://www.youtube.com/shorts/bbbbbbbbbbb" not in s.env.extracted()
    assert s.llm.calls == []


def test_nothing_to_do_calls_nothing(conn: sqlite3.Connection, settings: Settings) -> None:
    setup = Setup(conn, settings)  # 빈 보관함
    report = setup.worker().run_once()
    assert report.model_dump() == {
        "enriched": 0,
        "analyzed": 0,
        "assigned": 0,
        "indexed": 0,
        "failed": 0,
    }
    assert setup.env.log == [] and setup.llm.calls == []
    assert setup.embedder is not None and setup.embedder.calls == []


def test_instagram_block_pauses_instagram_but_youtube_continues(s: Setup) -> None:
    ig = s.shared_ig("CCCCCCCCCCC", BlockedError("HTTP Error 429: Too Many Requests"))
    yt = s.shared_yt("ddddddddddd")
    worker = s.worker()
    worker.run_once()

    assert s.status(yt) == "indexed"
    assert s.status(ig) == "failed"
    state = worker.state()
    assert set(state.paused) == {"instagram"}
    assert "429" in state.paused["instagram"].reason

    # 쉬는 동안에는 다시 요청하지 않아 시도 횟수를 아낀다
    requests = len(s.env.extracted())
    worker.run_once()
    assert len(s.env.extracted()) == requests
    assert s.env.item(ig)["attempts"] == 1

    # 쉬는 시간이 지나면 다시 시도
    s.clock.now = NOW + timedelta(seconds=s.settings.worker_backoff_sec + 1)
    s.env.responses[s.env.item(ig)["url"]] = ig_info("CCCCCCCCCCC")
    worker.run_once()
    assert s.status(ig) == "indexed"
    assert worker.state().paused == {}


def test_api_outage_pauses_ai_and_keeps_item_enriched(s: Setup) -> None:
    s.llm = FakeLLM(LLMUnavailableError("Claude API 529: 과부하"))
    item = s.shared_yt("eeeeeeeeeee")
    worker = s.worker()
    worker.run_once()
    assert s.status(item) == "enriched"  # 항목 잘못이 아니라 실패로 기록하지 않음
    assert "ai" in worker.state().paused

    calls = len(s.llm.calls)
    worker.run_once()
    assert len(s.llm.calls) == calls


def test_without_embedder_stops_at_analyzed_then_indexes_later(s: Setup) -> None:
    s.embedder = None
    item = s.shared_yt("fffffffffff")
    worker = s.worker()
    worker.run_once()
    assert s.status(item) == "analyzed"
    assert "의미 검색 모델" in (worker.state().notice or "")
    assert worker.state().pending > 0

    s.embedder = FakeEmbedder()
    worker.run_once()
    assert s.status(item) == "indexed"
    assert worker.state().pending == 0


def test_without_api_key_only_enriches(s: Setup) -> None:
    s.has_llm = False
    item = s.shared_yt("ggggggggggg")
    worker = s.worker()
    worker.run_once()
    assert s.status(item) == "enriched"
    assert "API 키" in (worker.state().notice or "")


def test_no_topics_yet_skips_organize_but_indexes(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    # 주제가 하나도 없으면 organize 가 전체 정리를 해 버리므로 워커는 건너뛴다
    setup = Setup(conn, settings)
    item = setup.shared_yt("hhhhhhhhhhh")
    setup.worker().run_once()
    assert setup.status(item) == "indexed"
    assert setup.topic_of(item) is None
    assert setup.organize_llm.calls == []


def test_trashed_shared_item_is_not_pending(s: Setup) -> None:
    item = s.shared_yt("iiiiiiiiiii")
    db.trash_items(s.conn, [item], now=NOW)
    assert db.count_unfinished(s.conn, SHARE_SOURCE, max_attempts=3) == 0


def test_thread_wakes_up_on_notify(s: Setup) -> None:
    worker = Worker(
        s.settings.model_copy(update={"worker_poll_sec": 3600}),
        WorkerDeps(
            enrich=s.env.deps(),
            llm=s.llm,
            organize_llm=s.organize_llm,
            embedder=lambda: s.embedder,
            now=s.clock,
        ),
    )
    worker.start()
    try:
        _wait(lambda: worker.state().last_run_at is not None)  # 시작하자마자 한 바퀴
        item = s.shared_yt("jjjjjjjjjjj")
        worker.notify()
        _wait(lambda: _status(s.settings, item) == "indexed")
    finally:
        worker.stop()


# --- 도우미 ---


def _status(settings: Settings, item_id: int) -> str:
    """워커 스레드가 쓴 결과를 새 연결로 읽는다."""
    c = db.connect(settings.db_path)
    try:
        return c.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    finally:
        c.close()


def _wait(cond: Callable[[], bool], timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("시간 안에 조건을 만족하지 않음")
