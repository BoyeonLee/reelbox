"""Typer 명령 모음. Phase 2까지: init, status, import instagram/urls, sync youtube, enrich."""

import logging
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from reelbox import db
from reelbox.config import Settings, get_settings
from reelbox.enrich import runner
from reelbox.enrich.transcribe import WhisperTranscriber
from reelbox.importers import ImportReport, instagram, urllist, youtube
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
    for noisy in ("httpx", "huggingface_hub"):
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


@app.command()
def analyze(limit: LimitOpt = None, dry_run: DryRunOpt = False) -> None:
    """AI 요약·태그."""
    _not_implemented("analyze")


@app.command()
def organize(
    full: Annotated[bool, typer.Option("--full", help="처음부터 다시 묶기")] = False,
) -> None:
    """주제 정리."""
    _not_implemented("organize")


@app.command()
def index() -> None:
    """임베딩·검색 색인."""
    _not_implemented("index")


@app.command()
def run() -> None:
    """enrich → analyze → organize → index 를 차례로."""
    _not_implemented("run")


@app.command()
def search(query: Annotated[str, typer.Argument(help="검색어")]) -> None:
    """터미널에서 검색 (확인용)."""
    _not_implemented("search")


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
