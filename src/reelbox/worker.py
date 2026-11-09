"""공유로 들어온 항목을 보강→분석→주제 배정→색인까지 자동 처리하는 워커 (Phase 6).

`reelbox serve` 안에서 스레드 하나로 돈다. 비유하면 주방(PC)에 들어온 주문서(공유한 링크)를
보는 요리사다. 새 주문이 오면(notify) 바로 일어나고, 아니면 worker_poll_sec 마다 남은 일이
있는지 확인한다.

- 공유·추가 화면으로 들어온 항목(imported_from='share')만 보강·분석한다. 아직 처리하지 않은
  내보내기 파일 항목까지 집으면 인스타 한도와 API 요금을 써 버리기 때문이다.
- 주제 배정과 색인은 CLI 와 같은 함수를 그대로 쓴다 (분석이 끝난 항목이면 누구든 대상).
- 인스타 차단·쿠키 오류, Claude API 오류가 나면 그 단계만 worker_backoff_sec 동안 쉰다.
- 임베딩 모델(bge-m3)은 서버가 불러온 것을 함께 쓴다. 아직이면 분석까지만 하고 다음에 색인.
"""

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from reelbox import db
from reelbox.ai import analyze as analyzer
from reelbox.ai import organize as organizer
from reelbox.ai.llm import LLM
from reelbox.config import Settings
from reelbox.enrich import runner
from reelbox.search import index as indexer
from reelbox.search.embed import Embedder

logger = logging.getLogger(__name__)

SHARE_SOURCE = "share"  # 화면·단축어로 추가한 항목의 imported_from
PLATFORMS = ("youtube", "instagram")  # 유튜브 먼저: 인스타는 요청마다 5~10초를 기다린다

# 단계 이름 (화면에 그대로 보인다)
Step = str
STEP_ENRICH = "보강"
STEP_ANALYZE = "분석"
STEP_ORGANIZE = "주제 정리"
STEP_INDEX = "색인"

EmbedderGetter = Callable[[], Embedder | None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class WorkerDeps:
    """워커가 쓰는 외부 도구. 테스트에서는 가짜로 바꾼다.

    llm·organize_llm 이 None 이면 (API 키 없음) 보강까지만 한다.
    """

    enrich: runner.EnrichDeps
    llm: LLM | None
    organize_llm: LLM | None
    embedder: EmbedderGetter
    now: Callable[[], datetime] = _utcnow


class Pause(BaseModel):
    reason: str
    until: str  # ISO 시각


class WorkerState(BaseModel):
    """화면(추가 화면)에 보여줄 워커 상태."""

    enabled: bool = True
    busy: bool = False
    step: Step | None = None  # 지금 하고 있는 단계
    pending: int = 0  # 처리가 남은 공유 항목 수
    paused: dict[str, Pause] = {}  # 쉬는 단계("instagram", "youtube", "ai") → 이유·재개 시각
    notice: str | None = None  # 멈춤은 아니지만 알려줄 것 (API 키 없음, 의미 검색 모델 준비 중)
    last_run_at: str | None = None


class RunReport(BaseModel):
    """run_once 한 번의 결과 (로그·테스트용)."""

    enriched: int = 0
    analyzed: int = 0
    assigned: int = 0
    indexed: int = 0
    failed: int = 0


class Worker:
    def __init__(self, settings: Settings, deps: WorkerDeps) -> None:
        self._settings = settings
        self._deps = deps
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()  # _state 를 웹 요청 스레드와 나눠 쓴다
        self._state = WorkerState()
        self._pause_until: dict[str, datetime] = {}  # 쉬는 단계 → 다시 할 시각
        self._pause_reasons: dict[str, str] = {}
        self._thread: threading.Thread | None = None

    # --- 스레드 ---

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="reelbox-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def notify(self) -> None:
        """새 항목이 들어왔다 → 기다리지 말고 바로 처리."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # 한 번 실패해도 서버가 켜져 있는 동안 계속 돌아야 한다
                logger.exception("워커 실행 중 오류")
                self._update(busy=False, step=None)
            self._wake.wait(self._settings.worker_poll_sec)
            self._wake.clear()

    # --- 상태 ---

    def state(self) -> WorkerState:
        with self._lock:
            return self._state.model_copy(deep=True)

    def _update(self, **fields: object) -> None:
        with self._lock:
            self._state = self._state.model_copy(update=fields)

    def _paused(self, key: str) -> bool:
        until = self._pause_until.get(key)
        if until is None:
            return False
        if self._deps.now() >= until:
            del self._pause_until[key]
            self._pause_reasons.pop(key, None)
            return False
        return True

    def _pause(self, key: str, reason: str) -> None:
        until = self._deps.now() + timedelta(seconds=self._settings.worker_backoff_sec)
        self._pause_until[key] = until
        self._pause_reasons[key] = reason
        logger.warning("워커: %s 처리를 %s 까지 쉼 — %s", key, db.to_iso(until), reason)

    def _publish_pauses(self) -> dict[str, Pause]:
        # 지난 멈춤은 지우고 남은 것만 화면에 보낸다
        for key in list(self._pause_until):
            self._paused(key)
        return {
            key: Pause(reason=self._pause_reasons.get(key, ""), until=db.to_iso(until))
            for key, until in self._pause_until.items()
        }

    # --- 한 번 돌기 ---

    def run_once(self) -> RunReport:
        """남은 일을 한 바퀴 처리한다. 할 일이 없으면 외부 도구를 하나도 부르지 않는다."""
        report = RunReport()
        conn = db.connect(self._settings.db_path)
        try:
            pending = self._pending(conn)
            if pending:
                self._update(busy=True, pending=pending)
                self._enrich(conn, report)
                self._analyze(conn, report)
                self._organize_and_index(conn, report)
            notice = self._notice()
            self._update(
                busy=False,
                step=None,
                pending=self._pending(conn),
                paused=self._publish_pauses(),
                notice=notice,
                last_run_at=db.to_iso(self._deps.now()),
            )
        finally:
            conn.close()
        if pending:
            logger.info("워커: %s", report.model_dump())
        return report

    def _pending(self, conn: sqlite3.Connection) -> int:
        """할 일: 공유 항목 중 안 끝난 것 + 주제·색인을 기다리는 분석 완료 항목."""
        unfinished = db.count_unfinished(
            conn, SHARE_SOURCE, max_attempts=self._settings.max_attempts
        )
        waiting_index = conn.execute(
            "SELECT COUNT(*) FROM items WHERE status = 'analyzed'"
        ).fetchone()[0]
        # 공유 항목의 analyzed 는 두 쪽에 다 세지만, 할 일이 있는지만 보면 된다
        return max(unfinished, waiting_index)

    def _notice(self) -> str | None:
        if self._deps.llm is None:
            return "Claude API 키가 없어서 보강까지만 해요 (.env 의 ANTHROPIC_API_KEY)"
        if self._deps.embedder() is None:
            return "의미 검색 모델을 불러오는 중이라, 색인은 조금 뒤에 해요"
        return None

    def _enrich(self, conn: sqlite3.Connection, report: RunReport) -> None:
        for platform in PLATFORMS:
            if self._paused(platform):
                continue
            if not db.enrich_candidates(
                conn,
                platform=platform,
                max_attempts=self._settings.max_attempts,
                limit=1,
                source=SHARE_SOURCE,
            ):
                continue
            self._update(step=STEP_ENRICH)
            result = runner.enrich_items(
                conn, self._settings, self._deps.enrich, platform=platform, source=SHARE_SOURCE
            )
            report.enriched += result.enriched
            report.failed += result.failed
            reason = result.stopped.get(platform)
            if reason:
                self._pause(platform, reason)

    def _analyze(self, conn: sqlite3.Connection, report: RunReport) -> None:
        llm = self._deps.llm
        if llm is None or self._paused("ai"):
            return
        if not db.analyze_candidates(
            conn, max_attempts=self._settings.max_attempts, limit=1, source=SHARE_SOURCE
        ):
            return
        self._update(step=STEP_ANALYZE)
        result = analyzer.analyze_items(conn, self._settings, llm, source=SHARE_SOURCE)
        report.analyzed += result.analyzed
        report.failed += result.failed
        if result.stopped:
            self._pause("ai", result.stopped)

    def _organize_and_index(self, conn: sqlite3.Connection, report: RunReport) -> None:
        embedder = self._deps.embedder()
        if embedder is None:
            return  # 모델을 불러오는 중. analyzed 로 두고 다음에
        organize_llm = self._deps.organize_llm
        # 주제가 하나도 없으면 organize 가 전체 정리를 해 버린다. 그건 CLI 로 직접 한다.
        has_new = bool(db.unassigned_items(conn, limit=1))
        if organize_llm is not None and has_new and db.list_topics(conn) and not self._paused("ai"):
            self._update(step=STEP_ORGANIZE)
            result = organizer.organize(conn, self._settings, organize_llm, embedder)
            report.assigned += result.assigned
            if result.stopped:
                # 주제를 못 받아도 검색은 되게 색인은 계속한다
                self._pause("ai", result.stopped)
        if db.index_candidates(conn, limit=1):
            self._update(step=STEP_INDEX)
            report.indexed += indexer.index_items(conn, self._settings, embedder).indexed
