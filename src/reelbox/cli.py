"""Typer 명령 모음. Phase 4까지: init, status, import, sync youtube, enrich, analyze, review,
organize, index, search."""

import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from reelbox import db
from reelbox.ai import analyze as analyzer
from reelbox.ai import organize as organizer
from reelbox.ai.llm import AnthropicLLM, LLMFatalError, LLMUnavailableError
from reelbox.config import Settings, get_settings
from reelbox.enrich import runner
from reelbox.enrich.transcribe import WhisperTranscriber
from reelbox.importers import ImportReport, instagram, urllist, youtube
from reelbox.search import index as indexer
from reelbox.search import query as searcher
from reelbox.search.embed import BgeM3Embedder
from reelbox.urls import Platform
from reelbox.ytdlp import CookieSource, YtdlpClient

app = typer.Typer(help="저장한 릴스·쇼츠를 AI로 요약·주제 분류하는 개인용 앱", no_args_is_help=True)
import_app = typer.Typer(help="내보내기 파일·목록 가져오기", no_args_is_help=True)
sync_app = typer.Typer(help="플랫폼 목록 동기화", no_args_is_help=True)
app.add_typer(import_app, name="import")
app.add_typer(sync_app, name="sync")

console = Console()

LimitOpt = Annotated[int | None, typer.Option("--limit", help="처리할 최대 항목 수")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="실제로 실행하지 않고 미리 보기")]


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="상세 로그")] = False,
) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    # whisper 모델을 받을 때 나오는 요청 로그(HTTP Request: ...)는 -v 에서도 숨긴다
    for noisy in ("httpx", "huggingface_hub", "sentence_transformers", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _not_implemented(command: str) -> None:
    console.print(f"[yellow]{command}: 아직 구현 안 됨[/yellow]")
    raise typer.Exit(code=1)


def _open_db(settings: Settings) -> sqlite3.Connection:
    if not settings.db_path.exists():
        console.print(f"[red]DB가 없음:[/red] {settings.db_path}\n먼저 [bold]reelbox init[/bold]")
        raise typer.Exit(code=1)
    return db.connect(settings.db_path)


def _print_report(report: ImportReport) -> None:
    console.print(f"[green]완료[/green] {report.summary()}")
    if report.unrecognized:
        console.print("[yellow]인식 실패 항목은 -v 로 다시 실행하면 로그에서 볼 수 있음[/yellow]")


@app.command()
def init() -> None:
    """DB 만들기·마이그레이션."""
    result = db.init_db(get_settings())
    if result.applied:
        for name in result.applied:
            console.print(f"[green]적용[/green] {name}")
        console.print(f"DB 준비 완료: {result.db_path} (스키마 버전 {result.version})")
    else:
        console.print(f"이미 최신: {result.db_path} (스키마 버전 {result.version})")


@app.command()
def status() -> None:
    """상태별 개수, 최근 실패."""
    conn = _open_db(get_settings())
    try:
        counts = db.status_counts(conn)
        failures = db.recent_failures(conn)
        version = db.schema_version(conn)
    finally:
        conn.close()

    table = Table(title=f"항목 상태 (스키마 버전 {version})")
    table.add_column("status")
    table.add_column("개수", justify="right")
    for name, n in counts.items():
        table.add_row(name, str(n))
    table.add_row("[bold]합계[/bold]", f"[bold]{sum(counts.values())}[/bold]")
    console.print(table)

    if failures:
        ftable = Table(title="최근 실패")
        for col in ("id", "platform", "url", "attempts", "error"):
            ftable.add_column(col)
        for f in failures:
            ftable.add_row(str(f.id), f.platform, f.url, str(f.attempts), f.error or "")
        console.print(ftable)
    else:
        console.print("최근 실패 없음")


@import_app.command("instagram")
def import_instagram(
    path: Annotated[Path, typer.Argument(help="내보내기 폴더 또는 zip")],
    limit: LimitOpt = None,
) -> None:
    """인스타 내보내기 가져오기."""
    conn = _open_db(get_settings())
    try:
        report = instagram.import_instagram(conn, path, limit=limit)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    _print_report(report)


@import_app.command("youtube")
def import_youtube(path: Annotated[Path, typer.Argument(help="테이크아웃 폴더 또는 zip")]) -> None:
    """테이크아웃(보조): 저장 시각 채우기."""
    _not_implemented("import youtube")


@import_app.command("urls")
def import_urls(
    path: Annotated[Path, typer.Argument(help="줄마다 링크 하나인 파일")],
    limit: LimitOpt = None,
) -> None:
    """링크 목록 가져오기."""
    if not path.is_file():
        console.print(f"[red]파일이 없음:[/red] {path}")
        raise typer.Exit(code=1)
    conn = _open_db(get_settings())
    try:
        report = urllist.import_urls(conn, path, limit=limit)
    finally:
        conn.close()
    _print_report(report)


@sync_app.command("youtube")
def sync_youtube(limit: LimitOpt = None, dry_run: DryRunOpt = False) -> None:
    """유튜브 '저장된 Shorts 동영상' 목록을 읽어 새 항목 추가."""
    settings = get_settings()
    url = settings.youtube_saved_shorts_url
    if not url:
        console.print(
            "[red]목록 주소가 없음.[/red] .env 에 REELBOX_YOUTUBE_SAVED_SHORTS_URL 을 넣는다."
        )
        raise typer.Exit(code=1)

    try:
        cookies = youtube.CookieSource.from_settings(settings)
        with console.status("저장된 Shorts 목록 읽는 중..."):
            entries = youtube.fetch_playlist(url, cookies, limit)
    except youtube.CookieError as exc:
        console.print(f"[red]쿠키 문제로 목록을 읽지 못함[/red]\n[bold]원인[/bold] {exc.cause}")
        console.print(f"[bold]해결[/bold] {exc.fix}")
        if exc.detail:
            console.print(f"[dim]yt-dlp: {exc.detail}[/dim]")
        raise typer.Exit(code=1) from exc
    except youtube.PlaylistError as exc:
        console.print(f"[red]목록을 읽지 못함:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if dry_run:
        table = Table(title=f"저장된 Shorts (앞에서 {len(entries)}개, 목록 순서)")
        table.add_column("#", justify="right")
        table.add_column("ID")
        table.add_column("제목")
        for i, entry in enumerate(entries, start=1):
            table.add_row(str(i), entry.video_id, entry.title or "")
        console.print(table)
        console.print("--dry-run: DB에 쓰지 않음")
        return

    if not entries:
        console.print("[yellow]목록이 비어 있음.[/yellow] 다른 계정의 쿠키인지 확인한다.")
    conn = _open_db(settings)
    try:
        report = youtube.add_entries(conn, entries)
    finally:
        conn.close()
    _print_report(report)


def _client_factory(settings: Settings) -> runner.ClientFactory:
    """플랫폼 → YtdlpClient. 인스타는 쿠키가 꼭 있어야 하고, 유튜브는 설정돼 있으면 쓴다."""

    def open_client(platform: Platform) -> YtdlpClient:
        if platform == "youtube" and not CookieSource.is_configured(settings, "youtube"):
            return YtdlpClient(None)  # 공개 쇼츠는 쿠키 없이도 읽힌다
        return YtdlpClient(CookieSource.from_settings(settings, platform))

    return open_client


def _print_enrich_report(report: runner.EnrichReport) -> None:
    table = Table(title="보강 결과")
    table.add_column("항목")
    table.add_column("개수", justify="right")
    table.add_row("[green]enriched[/green]", str(report.enriched))
    for source, n in sorted(report.sources.items()):
        table.add_row(f"  └ 내용 출처: {source}", str(n))
    table.add_row("[red]failed[/red]", str(report.failed))
    table.add_row("[yellow]unavailable[/yellow]", str(report.unavailable))
    if report.no_caption:
        table.add_row("캡션이 없어 건너뜀", str(report.no_caption))
    console.print(table)
    if report.tmp_cleaned:
        console.print(f"[dim]지난 실행에서 남은 임시 오디오 {report.tmp_cleaned}개를 지움[/dim]")
    for platform, reason in report.stopped.items():
        console.print(f"[red]{platform} 처리를 멈춤[/red] {reason}")
    if report.failed:
        console.print("실패 이유는 [bold]reelbox status[/bold] 에서 볼 수 있음")


@app.command()
def enrich(
    limit: LimitOpt = None,
    platform: Annotated[Platform | None, typer.Option(help="instagram 또는 youtube")] = None,
    captions_only: Annotated[
        bool,
        typer.Option(
            "--captions-only",
            help="인스타에 접속하지 않고 내보내기 파일의 캡션만으로 보강 완료 처리 (차단됐을 때)",
        ),
    ] = False,
) -> None:
    """메타·자막·받아쓰기."""
    settings = get_settings()
    conn = _open_db(settings)
    try:
        if captions_only:
            report = runner.enrich_from_captions(conn, settings, platform=platform, limit=limit)
        else:
            deps = runner.EnrichDeps(
                open_client=_client_factory(settings),
                transcriber=WhisperTranscriber(
                    settings.whisper_model, settings.whisper_device, settings.whisper_compute_type
                ),
            )
            with console.status("보강 준비 중...") as spinner:

                def on_item(n: int, total: int, item: db.EnrichCandidate) -> None:
                    spinner.update(f"보강 중 {n}/{total} [dim]{item.url}[/dim]")

                report = runner.enrich_items(
                    conn, settings, deps, platform=platform, limit=limit, on_item=on_item
                )
    finally:
        conn.close()
    _print_enrich_report(report)


def _require_api_key(settings: Settings) -> None:
    if settings.anthropic_api_key is None:
        console.print(
            "[red]Claude API 키가 없음.[/red] .env 에 ANTHROPIC_API_KEY=sk-ant-... 를 넣는다.\n"
            "키는 https://platform.claude.com 의 API Keys 에서 만든다 (API 요금은 claude.ai "
            "구독과 따로 충전)."
        )
        raise typer.Exit(code=1)


def _make_llm(settings: Settings) -> AnthropicLLM:
    _require_api_key(settings)
    return AnthropicLLM(
        settings.analyze_model,
        effort=settings.analyze_effort,
        max_tokens=settings.analyze_max_tokens,
        api_key=settings.anthropic_api_key,
    )


def _make_organize_llm(settings: Settings) -> AnthropicLLM:
    _require_api_key(settings)
    return AnthropicLLM(
        settings.organize_model,
        effort=settings.organize_effort,
        max_tokens=settings.organize_max_tokens,
        api_key=settings.anthropic_api_key,
    )


def _make_embedder(settings: Settings) -> BgeM3Embedder:
    """모델은 실제로 encode 할 때 불러온다 (CPU 에서 약 30초)."""
    return BgeM3Embedder(
        settings.embed_model,
        device=settings.embed_device,
        batch_size=settings.embed_batch_size,
        max_seq_length=settings.embed_max_seq_length,
    )


def _print_dry_run(report: analyzer.DryRunReport) -> None:
    table = Table(title="analyze --dry-run (모델은 부르지 않음)")
    table.add_column("항목")
    table.add_column("값", justify="right")
    table.add_row("분석할 항목", f"{report.items}개")
    table.add_row("모델 / 프롬프트", f"{report.model} / {report.prompt_version}")
    estimated = report.items - report.counted
    note = f" ({report.counted}개를 세고 {estimated}개는 평균으로 추정)" if estimated else ""
    table.add_row("입력 토큰", f"{report.input_tokens:,}{note}")
    table.add_row("출력 토큰 (추정)", f"{report.output_tokens:,}")
    table.add_row("예상 비용", f"약 ${report.cost_usd:.3f}")
    console.print(table)


def _print_analyze_report(report: analyzer.AnalyzeReport, settings: Settings) -> None:
    table = Table(title="분석 결과")
    table.add_column("항목")
    table.add_column("값", justify="right")
    table.add_row("[green]analyzed[/green]", str(report.analyzed))
    table.add_row("[red]failed[/red]", str(report.failed))
    if report.retried:
        table.add_row("형식 오류로 다시 부름", str(report.retried))
    table.add_row(
        "사용 토큰 (입력 / 출력)",
        f"{report.usage.input_tokens:,} / {report.usage.output_tokens:,}",
    )
    table.add_row("비용", f"약 ${analyzer.estimate_cost(settings, report.usage):.4f}")
    console.print(table)
    if report.stopped:
        console.print(f"[red]분석을 멈춤[/red] {report.stopped}")
    if report.failed:
        console.print("실패 이유는 [bold]reelbox status[/bold] 에서 볼 수 있음")


@app.command()
def analyze(limit: LimitOpt = None, dry_run: DryRunOpt = False) -> None:
    """AI 요약·태그."""
    settings = get_settings()
    llm = _make_llm(settings)
    conn = _open_db(settings)
    try:
        if dry_run:
            with console.status("토큰 세는 중..."):
                preview = analyzer.dry_run(conn, settings, llm, limit=limit)
            _print_dry_run(preview)
            return
        with console.status("분석 준비 중...") as spinner:

            def on_item(n: int, total: int, item: db.AnalyzeInput) -> None:
                spinner.update(f"분석 중 {n}/{total} [dim]{item.url}[/dim]")

            report = analyzer.analyze_items(conn, settings, llm, limit=limit, on_item=on_item)
    except (LLMFatalError, LLMUnavailableError) as exc:  # --dry-run 의 토큰 세기에서 남
        console.print(f"[red]Claude API 오류[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    _print_analyze_report(report, settings)


@app.command()
def review(
    limit: Annotated[int, typer.Option("--limit", help="보여줄 개수")] = 10,
) -> None:
    """최근 분석 결과를 표로 보기 (제목·요약·주제·태그)."""
    conn = _open_db(get_settings())
    try:
        rows = db.recent_analyses(conn, limit)
    finally:
        conn.close()
    if not rows:
        console.print("분석된 항목이 없음. 먼저 [bold]reelbox analyze[/bold]")
        return
    table = Table(title=f"최근 분석 {len(rows)}개", show_lines=True)
    table.add_column("id", justify="right")
    table.add_column("제목 / 작성자", max_width=28)
    table.add_column("요약", max_width=60)
    table.add_column("주제", max_width=16)
    table.add_column("태그", max_width=28)
    for r in rows:
        head = r.title or "(제목 없음)"
        if r.author:
            head += f"\n[dim]{r.author} · {r.platform}[/dim]"
        table.add_row(str(r.id), head, r.summary_ko or "", r.raw_topic or "", ", ".join(r.tags))
    console.print(table)


def _print_topics(conn: sqlite3.Connection) -> None:
    topics = db.topic_counts(conn)
    if not topics:
        return
    table = Table(title=f"주제 {len(topics)}개")
    table.add_column("id", justify="right")
    table.add_column("주제")
    table.add_column("영상", justify="right")
    table.add_column("설명", max_width=60)
    for t in topics:
        name = t.name + (" [cyan](고정)[/cyan]" if t.is_user_edited else "")
        table.add_row(str(t.id), name, str(t.items), t.description or "")
    console.print(table)


def _print_organize_report(report: organizer.OrganizeReport, settings: Settings) -> None:
    title = "전체 정리" if report.mode == "full" else "새 항목 배정"
    table = Table(title=f"주제 정리 결과 ({title})")
    table.add_column("항목")
    table.add_column("값", justify="right")
    if report.mode == "full":
        table.add_row("새로 만든 주제", str(report.topics_created))
        table.add_row("대응표로 배정", str(report.mapped))
    table.add_row("같은 raw_topic 주제 재사용", str(report.reused))
    table.add_row("후보 중 Claude 가 고름", str(report.by_llm))
    if report.no_fit:
        table.add_row("[yellow]어울리는 후보 없음 → 가장 가까운 주제[/yellow]", str(report.no_fit))
    if report.left:
        table.add_row("[red]주제를 못 받음 (다음에 다시)[/red]", str(report.left))
    if report.embedded:
        table.add_row("새로 계산한 임베딩", str(report.embedded))
    table.add_row(
        "사용 토큰 (입력 / 출력)",
        f"{report.usage.input_tokens:,} / {report.usage.output_tokens:,}",
    )
    # 가격 설정은 analyze 모델 기준이라 organize 모델이 다르면 대략값이다
    table.add_row("비용 (대략)", f"약 ${analyzer.estimate_cost(settings, report.usage):.4f}")
    console.print(table)
    if report.stopped:
        console.print(f"[red]주제 정리를 멈춤[/red] {report.stopped}")
    if report.no_fit and report.no_fit * 5 >= report.assigned:
        console.print(
            "어울리는 주제가 없는 항목이 많음. "
            "[bold]reelbox organize --full[/bold] 로 다시 묶어 볼 것"
        )


@app.command()
def organize(
    full: Annotated[bool, typer.Option("--full", help="처음부터 다시 묶기")] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", help="새 항목 배정에서 처리할 최대 항목 수")
    ] = None,
) -> None:
    """주제 정리. 주제가 없으면 전체 정리, 있으면 새 항목만 기존 주제에 배정."""
    settings = get_settings()
    llm = _make_organize_llm(settings)
    embedder = _make_embedder(settings)
    conn = _open_db(settings)
    try:
        with console.status("주제 정리 준비 중...") as spinner:
            report = organizer.organize(
                conn,
                settings,
                llm,
                embedder,
                full=full,
                limit=limit,
                on_status=lambda msg: spinner.update(msg),
            )
        _print_organize_report(report, settings)
        _print_topics(conn)
    finally:
        conn.close()


@app.command()
def index(limit: LimitOpt = None) -> None:
    """임베딩·검색 색인."""
    settings = get_settings()
    embedder = _make_embedder(settings)
    conn = _open_db(settings)
    try:
        with console.status("색인 준비 중 (처음엔 모델을 불러오느라 30초쯤 걸림)...") as spinner:

            def on_progress(done: int, total: int) -> None:
                spinner.update(f"임베딩 계산 중 {done}/{total}")

            report = indexer.index_items(
                conn, settings, embedder, limit=limit, on_progress=on_progress
            )
    finally:
        conn.close()
    table = Table(title="색인 결과")
    table.add_column("항목")
    table.add_column("개수", justify="right")
    table.add_row("[green]indexed[/green]", str(report.indexed))
    table.add_row("  └ 새로 계산한 임베딩", str(report.embedded))
    table.add_row("  └ organize 가 계산해 둔 임베딩 재사용", str(report.reused))
    table.add_row("비슷한 영상을 계산한 항목", str(report.similar_items))
    console.print(table)


SEARCH_SUMMARY_CHARS = 90  # 터미널 표가 너무 길어지지 않게
_LIST_KO = {"keyword": "키워드", "short": "짧은 단어", "vector": "의미"}


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="검색어")],
    limit: Annotated[int, typer.Option("--limit", help="보여줄 개수")] = 10,
    platform: Annotated[Platform | None, typer.Option(help="instagram 또는 youtube")] = None,
    topic: Annotated[str | None, typer.Option(help="주제 이름 또는 id")] = None,
    since: Annotated[str | None, typer.Option(help="이날 이후 저장한 것 (YYYY-MM-DD)")] = None,
    until: Annotated[str | None, typer.Option(help="이날까지 저장한 것 (YYYY-MM-DD)")] = None,
    favorite: Annotated[bool, typer.Option("--favorite", help="즐겨찾기만")] = False,
    keyword_only: Annotated[
        bool, typer.Option("--keyword-only", help="의미 검색 없이 키워드만 (모델을 안 불러서 빠름)")
    ] = False,
) -> None:
    """터미널에서 검색 (확인용). 키워드와 의미 검색 결과를 RRF 로 합친다."""
    try:
        filters = searcher.SearchFilters(
            platform=platform,
            topic=topic,
            since=date.fromisoformat(since) if since else None,
            until=date.fromisoformat(until) if until else None,
            favorite=favorite,
        )
    except ValueError as exc:
        console.print(f"[red]날짜 형식이 틀림 (YYYY-MM-DD):[/red] {exc}")
        raise typer.Exit(code=1) from exc
    settings = get_settings()
    embedder = None if keyword_only else _make_embedder(settings)
    conn = _open_db(settings)
    try:
        message = "검색 중..." if keyword_only else "검색 중 (모델 불러오느라 30초쯤 걸림)..."
        with console.status(message):
            hits = searcher.search(conn, query, embedder=embedder, filters=filters, limit=limit)
    finally:
        conn.close()
    if not hits:
        console.print("결과 없음. 색인이 비었으면 먼저 [bold]reelbox index[/bold]")
        return
    table = Table(title=f"'{query}' 검색 결과 {len(hits)}개", show_lines=True)
    table.add_column("#", justify="right")
    table.add_column("제목 / 작성자", max_width=30)
    table.add_column("주제", max_width=12)
    table.add_column("요약", max_width=50)
    table.add_column("걸린 곳 (순위)", max_width=18)
    for n, h in enumerate(hits, start=1):
        head = h.title or "(제목 없음)"
        head += f"\n[dim]{h.author or ''} · {h.platform}[/dim]\n[link={h.url}]{h.url}[/link]"
        where = "\n".join(f"{_LIST_KO.get(k, k)} {r}위" for k, r in h.ranks.items())
        summary = h.summary_ko or ""
        if len(summary) > SEARCH_SUMMARY_CHARS:
            summary = summary[:SEARCH_SUMMARY_CHARS] + "…"
        table.add_row(str(n), head, h.topic or "", summary, where)
    console.print(table)


@app.command()
def run() -> None:
    """enrich → analyze → organize → index 를 차례로."""
    _not_implemented("run")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="바인딩 주소")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="포트")] = 8000,
) -> None:
    """웹 서버 + 백그라운드 워커."""
    _not_implemented("serve")


@app.command()
def purge() -> None:
    """휴지통에서 30일 지난 항목 영구 정리."""
    _not_implemented("purge")


@app.command()
def publish(dry_run: DryRunOpt = False) -> None:
    """스냅샷을 만들어 보기 전용 사이트에 배포."""
    _not_implemented("publish")
