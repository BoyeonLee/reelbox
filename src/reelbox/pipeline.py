"""`reelbox run`: enrich → analyze → organize → index 를 차례로 (DESIGN.md 7장, Phase 8 결정).

새 내보내기 항목을 언제 보강할지는 내가 정한다 (약 100개만 먼저 처리하기로 함, 8장).
- `run`           : 워커와 같은 규칙. 공유로 들어온 것과 "다시 시도"를 누른 것만 보강·분석한다.
                    아직 안 한 내보내기 항목은 건드리지 않는다 (인스타 한도·API 요금 보호).
- `run --limit N` : 아직 안 한 내보내기 항목까지 포함해 N개를 새로 보강·분석한다.
주제 배정·색인은 분석이 끝난 항목이면 출처와 상관없이 한다. publish 는 cli.run 이 마지막에 부른다.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from reelbox import db
from reelbox.ai import analyze as analyzer
from reelbox.ai import organize as organizer
from reelbox.ai.llm import LLM
from reelbox.config import Settings
from reelbox.enrich import runner
from reelbox.search import index as indexer
from reelbox.search.embed import Embedder
from reelbox.worker import SHARE_SOURCE

StatusFn = Callable[[str], None]


@dataclass
class PipelineDeps:
    """run 이 쓰는 외부 도구. 테스트에서는 가짜로 바꾼다. 임베딩 모델은 쓸 때만 불러온다."""

    enrich: runner.EnrichDeps
    llm: LLM
    organize_llm: LLM
    embedder: Callable[[], Embedder]


class PipelineReport(BaseModel):
    enrich: runner.EnrichReport
    analyze: analyzer.AnalyzeReport
    organize: organizer.OrganizeReport | None  # None: 배정할 새 항목이 없거나 주제가 아직 없음
    organize_skipped_no_topics: bool = False
    index: indexer.IndexReport


def run_pipeline(
    conn: sqlite3.Connection,
    settings: Settings,
    deps: PipelineDeps,
    *,
    limit: int | None,
    on_status: StatusFn = lambda _: None,
) -> PipelineReport:
    # limit 이 없으면 공유·다시 시도 항목만 (source 필터), 있으면 출처 상관없이 N개
    source = SHARE_SOURCE if limit is None else None

    on_status("보강 중 (인스타그램은 요청 간격을 지켜서 느려요)...")
    enriched = runner.enrich_items(conn, settings, deps.enrich, limit=limit, source=source)

    on_status("AI 분석 중...")
    analyzed = analyzer.analyze_items(conn, settings, deps.llm, limit=limit, source=source)

    embedder: Embedder | None = None
    organized: organizer.OrganizeReport | None = None
    skipped = False
    if db.unassigned_items(conn, limit=1):
        if db.list_topics(conn):
            on_status("주제 배정 중 (처음엔 모델을 불러오느라 30초쯤 걸림)...")
            embedder = deps.embedder()
            organized = organizer.organize(conn, settings, deps.organize_llm, embedder)
        else:
            # 주제가 하나도 없으면 organize 가 전체 정리를 해 버린다. 그건 직접 실행한다.
            skipped = True

    indexed = indexer.IndexReport()
    if db.index_candidates(conn, limit=1):
        on_status("색인 중...")
        indexed = indexer.index_items(conn, settings, embedder or deps.embedder())

    return PipelineReport(
        enrich=enriched,
        analyze=analyzed,
        organize=organized,
        organize_skipped_no_topics=skipped,
        index=indexed,
    )
