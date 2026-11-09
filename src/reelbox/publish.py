"""보기 전용 사이트: 스냅샷 만들기·Cloudflare Pages 배포 (DESIGN.md 6장 "보기 전용 사이트").

비유: PC(주방)가 닫혀 있을 때 문 앞에 붙여 둘 메뉴판을 만드는 일.

data/dist/
├─ index.html, app.js, data.js, … (web/static 화면 파일 그대로)
├─ snapshot/items.json   숨김·삭제가 아닌, 분석이 끝난 영상 (자막 원문 없음)
├─ snapshot/topics.json  주제 이름
├─ snapshot/meta.json    마지막 업데이트 시각, PC 주소
└─ thumbs/*.webp         스냅샷 영상의 썸네일 (PC 의 /thumbs/ 와 같은 이름)

순서: 스냅샷 만들기 → dist 쓰기 → 지난번 배포와 내용이 같으면 멈춤 → Access 가 켜져 있는지 확인
→ wrangler 로 배포 → 배포한 내용의 지문(hash)을 기록.
"""

import fcntl
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel

from reelbox import db
from reelbox.config import Settings

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"
SNAPSHOT_DIR = "snapshot"
META_FILE = f"{SNAPSHOT_DIR}/meta.json"
ACCESS_HOST_SUFFIX = ".cloudflareaccess.com"
# 배포할 때마다 생기는 <해시>.<프로젝트>.pages.dev 주소도 잠겼는지 보려고 두드려 보는 하위 주소
ACCESS_PROBE_SUBDOMAIN = "reelbox-access-check"
WRANGLER_TIMEOUT_SEC = 600


class PublishError(RuntimeError):
    """배포를 멈춰야 하는 문제 (Access 꺼짐, wrangler 실패 등). 메시지는 화면에 그대로 보여준다."""


# --- 스냅샷 내용 ---


class SnapshotItem(BaseModel):
    """스냅샷의 영상 하나. 화면(data.js)이 API 의 Card/Detail 모양으로 바꿔 쓴다.

    자막 원문(transcript)·원본 설명(description)은 일부러 필드가 없다 — 용량과 개인정보·저작권.
    """

    id: int
    platform: str
    url: str
    title: str | None
    author: str | None
    summary: str | None
    key_points: list[str]
    tags: list[str]
    topic_id: int | None
    topic: str | None
    saved_at: str
    duration_sec: float | None
    thumb: str | None  # "/thumbs/<이름>.webp" (PC API 와 같은 모양)
    media_type: str | None
    status: str
    favorite: bool
    note: str | None
    similar: list[int]  # 비슷한 영상 id (스냅샷에 있는 것만)


class SnapshotTopic(BaseModel):
    id: int
    name: str
    description: str | None


class Snapshot(BaseModel):
    items: list[SnapshotItem]
    topics: list[SnapshotTopic]
    thumb_paths: list[str]  # 복사할 썸네일 (data_dir 기준 경로)


class SnapshotMeta(BaseModel):
    generated_at: str  # 마지막 업데이트 시각 (화면의 "마지막 업데이트 N일 전")
    pc_url: str | None  # 화면이 먼저 연결해 볼 PC 주소
    items: int
    topics: int


def _thumb(path: str | None) -> str | None:
    return f"/thumbs/{Path(path).name}" if path else None


def build_snapshot(conn: sqlite3.Connection) -> Snapshot:
    """목록에 보이는 항목(분석 끝 + 숨김·삭제 아님)만 담는다. 홈 화면과 같은 기준이다."""
    cards = db.list_items(conn, limit=-1)  # SQLite 에서 LIMIT -1 은 "제한 없음"
    ids = {c.id for c in cards}
    items: list[SnapshotItem] = []
    thumb_paths: list[str] = []
    for card in cards:
        d = db.get_item_detail(conn, card.id)
        if d is None:
            continue
        items.append(
            SnapshotItem(
                id=d.id,
                platform=d.platform,
                url=d.url,
                title=d.title,
                author=d.author,
                summary=d.summary_ko,
                key_points=d.key_points,
                tags=d.tags,
                topic_id=d.topic_id,
                topic=d.topic,
                saved_at=d.saved_at or d.created_at,
                duration_sec=d.duration_sec,
                thumb=_thumb(d.thumbnail_path),
                media_type=d.media_type,
                status=d.status,
                favorite=d.favorite,
                note=d.note,
                similar=[s.id for s in d.similar if s.id in ids],
            )
        )
        if d.thumbnail_path:
            thumb_paths.append(d.thumbnail_path)
    used_topics = {i.topic_id for i in items if i.topic_id is not None}
    topics = [
        SnapshotTopic(id=t.id, name=t.name, description=t.description)
        for t in db.topic_counts(conn)
        if t.id in used_topics
    ]
    return Snapshot(items=items, topics=topics, thumb_paths=thumb_paths)


# --- data/dist 쓰기 ---


class BuildReport(BaseModel):
    items: int
    topics: int
    thumbs: int
    files: int
    bytes: int
    content_hash: str  # 업데이트 시각을 뺀 내용의 지문. 같으면 배포하지 않는다


def _dump(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii=False: 한글을 \uXXXX 로 바꾸지 않아 파일이 작다. separators: 공백 없이
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _files(root: Path) -> Iterator[Path]:
    return (p for p in sorted(root.rglob("*")) if p.is_file())


def content_hash(root: Path, pc_url: str | None) -> str:
    """dist 의 모든 파일(meta.json 제외) 이름과 내용으로 만든 지문 + PC 주소."""
    h = hashlib.sha256()
    h.update((pc_url or "").encode())
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        if rel == META_FILE:
            continue  # 업데이트 시각만 바뀐 것은 "바뀐 것"으로 치지 않는다
        h.update(b"\0" + rel.encode() + b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()


def write_dist(
    snapshot: Snapshot, settings: Settings, dest: Path, *, generated_at: datetime
) -> BuildReport:
    """dest 를 비우고 화면 파일 + 스냅샷 JSON + 썸네일을 쓴다."""
    if dest.exists():
        shutil.rmtree(dest)
    # 화면 파일은 PC 서버가 보내는 것과 똑같다 (화면은 하나, data.js 가 채널만 바꾼다)
    shutil.copytree(STATIC_DIR, dest, ignore=shutil.ignore_patterns(".*", "__pycache__"))
    _dump(dest / SNAPSHOT_DIR / "items.json", [i.model_dump() for i in snapshot.items])
    _dump(dest / SNAPSHOT_DIR / "topics.json", [t.model_dump() for t in snapshot.topics])
    copied = 0
    for rel in snapshot.thumb_paths:
        src = settings.data_dir / rel
        if src.is_file():
            target = dest / "thumbs" / src.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            copied += 1
        else:
            logger.warning("썸네일 파일이 없어 건너뜀: %s", rel)
    meta = SnapshotMeta(
        generated_at=db.to_iso(generated_at),
        pc_url=settings.pc_url,
        items=len(snapshot.items),
        topics=len(snapshot.topics),
    )
    _dump(dest / META_FILE, meta.model_dump())
    files = list(_files(dest))
    return BuildReport(
        items=len(snapshot.items),
        topics=len(snapshot.topics),
        thumbs=copied,
        files=len(files),
        bytes=sum(p.stat().st_size for p in files),
        content_hash=content_hash(dest, settings.pc_url),
    )


# --- 지난번 배포 기록 ---


class PublishState(BaseModel):
    content_hash: str
    published_at: str


def load_state(settings: Settings) -> PublishState | None:
    try:
        return PublishState.model_validate_json(settings.publish_state_path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def save_state(settings: Settings, state: PublishState) -> None:
    settings.publish_state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.publish_state_path.write_text(state.model_dump_json(), encoding="utf-8")


# --- 외부 호출 래퍼 (테스트에서는 가짜로 바꾼다) ---

# 주소 → (상태 코드, Location 헤더). 리다이렉트는 따라가지 않는다.
HttpGet = Callable[[str], tuple[int, str | None]]
# dist 폴더를 Pages 에 올린다. 실패하면 PublishError.
Deployer = Callable[[Path, Settings], None]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # 따라가지 않으면 urllib 이 3xx 를 HTTPError 로 올린다


def http_get(url: str) -> tuple[int, str | None]:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "reelbox-publish"})
    try:
        with opener.open(request, timeout=15) as res:
            return res.status, res.headers.get("Location")
    except urllib.error.HTTPError as exc:  # 3xx·4xx·5xx 는 여기로 온다
        return exc.code, exc.headers.get("Location")


def wrangler_deploy(dist: Path, settings: Settings) -> None:
    """npx wrangler pages deploy. Node.js 가 필요하다 (처음엔 wrangler 를 받느라 조금 걸린다)."""
    if shutil.which("npx") is None:
        raise PublishError("npx 가 없어요. Node.js 를 설치해 주세요 (docs/cloudflare-setup.md)")
    env = dict(os.environ)
    if settings.cloudflare_api_token is not None:
        env["CLOUDFLARE_API_TOKEN"] = settings.cloudflare_api_token.get_secret_value()
    if settings.cloudflare_account_id:
        env["CLOUDFLARE_ACCOUNT_ID"] = settings.cloudflare_account_id
    cmd = [
        "npx",
        "--yes",
        settings.wrangler_package,
        "pages",
        "deploy",
        str(dist),
        "--project-name",
        str(settings.pages_project),
        "--branch",
        settings.pages_branch,
        "--commit-dirty=true",  # git 저장소가 아니어도 경고 없이
    ]
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=WRANGLER_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError("wrangler 배포가 10분 안에 끝나지 않았어요") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip()[-800:]
        raise PublishError(f"wrangler 배포 실패: {tail}")
    logger.info("wrangler: %s", result.stdout.strip()[-400:])


def _locked_by_access(status: int, location: str | None) -> bool:
    if status not in (301, 302, 303, 307, 308) or not location:
        return False
    host = urlsplit(location).hostname or ""
    return host.endswith(ACCESS_HOST_SUFFIX)


def check_access(pages_url: str, get: HttpGet = http_get) -> list[str]:
    """로그인 없이 열어서 Access 로그인 화면으로 넘어가는지 본다. 문제 목록을 돌려준다(비면 통과).

    배포할 때마다 생기는 <해시>.<프로젝트>.pages.dev 주소도 잠겨야 해서, 없는 하위 주소도 두드린다.
    """
    host = urlsplit(pages_url).hostname
    if not host:
        return [f"REELBOX_PAGES_URL 이 주소 모양이 아니에요: {pages_url}"]
    urls = [f"https://{host}/", f"https://{ACCESS_PROBE_SUBDOMAIN}.{host}/"]
    problems: list[str] = []
    for url in urls:
        try:
            status, location = get(url)
        except OSError as exc:
            problems.append(f"{url} 에 연결하지 못했어요 ({exc})")
            continue
        if not _locked_by_access(status, location):
            problems.append(f"{url} 이 Access 로그인으로 넘어가지 않아요 (응답 {status})")
    return problems


# --- publish ---


class PublishResult(StrEnum):
    DRY_RUN = "dry_run"  # --dry-run: 파일 수·용량만
    BUILT = "built"  # Pages 설정이 없어 data/dist 만 만듦
    UNCHANGED = "unchanged"  # 지난번 배포와 같아서 안 올림
    DEPLOYED = "deployed"


class PublishReport(BaseModel):
    result: PublishResult
    build: BuildReport
    dist: Path


def pages_configured(settings: Settings) -> bool:
    return bool(settings.pages_project and settings.pages_url)


@contextmanager
def _publish_lock(settings: Settings) -> Iterator[None]:
    """run 과 serve 의 자동 publish 가 동시에 dist 를 쓰지 않게 파일 잠금을 건다."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.data_dir / "publish.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # 다른 쪽이 끝날 때까지 기다린다
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def publish(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime,
    dry_run: bool = False,
    force: bool = False,
    get: HttpGet | None = None,
    deploy: Deployer | None = None,
) -> PublishReport:
    """스냅샷을 만들어 배포한다. 바뀐 게 없으면(force 가 아니면) 올리지 않는다.

    get·deploy 를 안 주면 실제 도구(http_get, wrangler_deploy)를 쓴다. 기본값을 인자 자리에
    바로 쓰지 않는 이유: 파이썬 기본 인자는 함수를 정의할 때 한 번 묶여서, 테스트가 모듈의
    함수를 바꿔치기해도 반영되지 않는다.
    """
    get = get or http_get
    deploy = deploy or wrangler_deploy
    snapshot = build_snapshot(conn)
    if dry_run:
        # 임시 폴더에 만들어 보고 지운다 (data/dist 는 건드리지 않음)
        with tempfile.TemporaryDirectory(prefix="reelbox-dist-") as tmp:
            build = write_dist(snapshot, settings, Path(tmp) / "dist", generated_at=now)
        return PublishReport(result=PublishResult.DRY_RUN, build=build, dist=settings.dist_dir)

    with _publish_lock(settings):
        build = write_dist(snapshot, settings, settings.dist_dir, generated_at=now)
        report = PublishReport(result=PublishResult.BUILT, build=build, dist=settings.dist_dir)
        if not pages_configured(settings):
            return report
        last = load_state(settings)
        if not force and last is not None and last.content_hash == build.content_hash:
            return report.model_copy(update={"result": PublishResult.UNCHANGED})
        assert settings.pages_url is not None
        problems = check_access(settings.pages_url, get)
        if problems:
            raise PublishError(
                "Cloudflare Access 가 켜져 있지 않아 배포하지 않았어요 "
                "(docs/cloudflare-setup.md 4단계).\n- " + "\n- ".join(problems)
            )
        deploy(settings.dist_dir, settings)
        save_state(
            settings, PublishState(content_hash=build.content_hash, published_at=db.to_iso(now))
        )
        return report.model_copy(update={"result": PublishResult.DEPLOYED})
