"""Typer 명령 모음. Phase 0에서는 init, status 만 동작한다."""

import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from reelbox import db
from reelbox.config import get_settings

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


def _not_implemented(command: str) -> None:
    console.print(f"[yellow]{command}: 아직 구현 안 됨[/yellow]")
    raise typer.Exit(code=1)


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
    settings = get_settings()
    if not settings.db_path.exists():
        console.print(f"[red]DB가 없음:[/red] {settings.db_path}\n먼저 [bold]reelbox init[/bold]")
        raise typer.Exit(code=1)

    conn = db.connect(settings.db_path)
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
def import_instagram(path: Annotated[Path, typer.Argument(help="내보내기 폴더 또는 zip")]) -> None:
    """인스타 내보내기 가져오기."""
    _not_implemented("import instagram")


@import_app.command("youtube")
def import_youtube(path: Annotated[Path, typer.Argument(help="테이크아웃 폴더 또는 zip")]) -> None:
    """테이크아웃(보조): 저장 시각 채우기."""
    _not_implemented("import youtube")


@import_app.command("urls")
def import_urls(path: Annotated[Path, typer.Argument(help="줄마다 링크 하나인 파일")]) -> None:
    """링크 목록 가져오기."""
    _not_implemented("import urls")


@sync_app.command("youtube")
def sync_youtube() -> None:
    """유튜브 '저장된 Shorts 동영상' 목록을 읽어 새 항목 추가."""
    _not_implemented("sync youtube")


@app.command()
def enrich(
    limit: LimitOpt = None,
    platform: Annotated[str | None, typer.Option(help="instagram 또는 youtube")] = None,
) -> None:
    """메타·자막·받아쓰기."""
    _not_implemented("enrich")


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
