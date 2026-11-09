"""보기 전용 사이트 스냅샷·배포 (DESIGN.md 6장). Cloudflare·wrangler 는 가짜로 바꾼다."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reelbox import publish as pub
from reelbox.config import Settings

from .conftest import analyzed_item, set_user_meta

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
SECRET_TRANSCRIPT = "자막원문비밀문장"
ACCESS_LOGIN = "https://me.cloudflareaccess.com/cdn-cgi/access/login/reelbox.pages.dev"


@pytest.fixture
def library(conn: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    thumbs = settings.thumbs_dir
    thumbs.mkdir(parents=True)
    for name in ("youtube_aaaaaaaaaaa", "youtube_hhhhhhhhhhh"):
        (thumbs / f"{name}.webp").write_bytes(b"webp-" + name.encode())
    ids = {
        "pasta": analyzed_item(
            conn,
            "aaaaaaaaaaa",
            title="브라운버터 파스타",
            summary="버터를 갈색으로 녹여 만드는 파스타",
            tags=["파스타"],
            transcript=SECRET_TRANSCRIPT,
            saved_at="2026-10-01T00:00:00Z",
            thumbnail_path="thumbs/youtube_aaaaaaaaaaa.webp",
        ),
        "tomato": analyzed_item(conn, "bbbbbbbbbbb", title="토마토 파스타", status="indexed"),
        "hidden": analyzed_item(
            conn,
            "hhhhhhhhhhh",
            title="숨긴 영상",
            thumbnail_path="thumbs/youtube_hhhhhhhhhhh.webp",
        ),
        "trashed": analyzed_item(conn, "ddddddddddd", title="삭제한 영상"),
        "waiting": analyzed_item(conn, "eeeeeeeeeee", title="분석 전", status="enriched"),
    }
    set_user_meta(conn, ids["hidden"], hidden_at="2026-11-01T00:00:00Z")
    set_user_meta(conn, ids["trashed"], deleted_at="2026-11-01T00:00:00Z")
    set_user_meta(conn, ids["pasta"], favorite=1, note="주말에 해 보기")
    cook = conn.execute("INSERT INTO topics (name) VALUES ('요리')").lastrowid
    empty = conn.execute("INSERT INTO topics (name) VALUES ('빈 주제')").lastrowid
    assert cook and empty
    conn.executemany(
        "INSERT INTO item_topics (item_id, topic_id, is_primary) VALUES (?, ?, 1)",
        [(ids["pasta"], cook), (ids["tomato"], cook), (ids["hidden"], empty)],
    )
    conn.executemany(
        "INSERT INTO item_similar (item_id, similar_id, rank, score) VALUES (?, ?, ?, 0.9)",
        [(ids["pasta"], ids["hidden"], 1), (ids["pasta"], ids["tomato"], 2)],
    )
    conn.commit()
    return ids


@pytest.fixture
def pages(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "pages_project": "reelbox",
            "pages_url": "https://reelbox.pages.dev",
            "pc_url": "https://laptop.tail1234.ts.net",
        }
    )


def locked(url: str) -> tuple[int, str | None]:
    return 302, ACCESS_LOGIN


class FakeDeploy:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, dist: Path, settings: Settings) -> None:
        assert (dist / "index.html").is_file()
        self.calls.append(dist)


def _read(dist: Path, name: str) -> object:
    return json.loads((dist / "snapshot" / name).read_text("utf-8"))


# --- 스냅샷 내용 ---


def test_snapshot_keeps_only_visible_analyzed_items(
    conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    snap = pub.build_snapshot(conn)
    assert {i.id for i in snap.items} == {library["pasta"], library["tomato"]}
    # 숨김 항목만 있는 주제는 빠진다
    assert [t.name for t in snap.topics] == ["요리"]


def test_snapshot_item_fields(conn: sqlite3.Connection, library: dict[str, int]) -> None:
    pasta = next(i for i in pub.build_snapshot(conn).items if i.id == library["pasta"])
    assert pasta.favorite is True
    assert pasta.note == "주말에 해 보기"
    assert pasta.tags == ["파스타"]
    assert pasta.topic == "요리"
    assert pasta.thumb == "/thumbs/youtube_aaaaaaaaaaa.webp"
    # 숨긴 영상은 비슷한 영상에서도 빠진다
    assert pasta.similar == [library["tomato"]]


def test_dist_has_shell_snapshot_and_thumbs_but_no_transcript(
    conn: sqlite3.Connection, library: dict[str, int], pages: Settings
) -> None:
    report = pub.publish(conn, pages, now=NOW, get=locked, deploy=FakeDeploy())
    dist = pages.dist_dir
    assert (dist / "index.html").is_file()
    assert (dist / "data.js").is_file()
    assert (dist / "search_ko.js").is_file()
    # 쓰는 썸네일만 복사 (숨긴 영상 썸네일은 없음)
    assert sorted(p.name for p in (dist / "thumbs").iterdir()) == ["youtube_aaaaaaaaaaa.webp"]
    meta = _read(dist, "meta.json")
    assert meta == {
        "generated_at": "2026-11-09T12:00:00Z",
        "pc_url": "https://laptop.tail1234.ts.net",
        "items": 2,
        "topics": 1,
    }
    assert len(_read(dist, "items.json")) == 2  # type: ignore[arg-type]
    everything = "".join(p.read_text("utf-8", errors="ignore") for p in dist.rglob("*.json"))
    assert SECRET_TRANSCRIPT not in everything
    assert "숨긴 영상" not in everything
    assert "삭제한 영상" not in everything
    assert report.build.files == sum(1 for p in dist.rglob("*") if p.is_file())


# --- 배포 ---


def test_deploys_then_skips_when_nothing_changed(
    conn: sqlite3.Connection, library: dict[str, int], pages: Settings
) -> None:
    deploy = FakeDeploy()
    first = pub.publish(conn, pages, now=NOW, get=locked, deploy=deploy)
    assert first.result is pub.PublishResult.DEPLOYED
    # 시각만 바뀌고 내용은 같음 → 올리지 않는다
    second = pub.publish(conn, pages, now=LATER, get=locked, deploy=deploy)
    assert second.result is pub.PublishResult.UNCHANGED
    assert len(deploy.calls) == 1
    # 즐겨찾기를 바꾸면 다시 올린다
    conn.execute("UPDATE user_meta SET favorite = 0 WHERE item_id = ?", (library["pasta"],))
    conn.commit()
    third = pub.publish(conn, pages, now=LATER, get=locked, deploy=deploy)
    assert third.result is pub.PublishResult.DEPLOYED
    assert len(deploy.calls) == 2


def test_force_deploys_even_if_unchanged(
    conn: sqlite3.Connection, library: dict[str, int], pages: Settings
) -> None:
    deploy = FakeDeploy()
    pub.publish(conn, pages, now=NOW, get=locked, deploy=deploy)
    again = pub.publish(conn, pages, now=NOW, get=locked, deploy=deploy, force=True)
    assert again.result is pub.PublishResult.DEPLOYED
    assert len(deploy.calls) == 2


def test_refuses_to_deploy_without_access(
    conn: sqlite3.Connection, library: dict[str, int], pages: Settings
) -> None:
    seen: list[str] = []

    def open_site(url: str) -> tuple[int, str | None]:
        seen.append(url)
        return 200, None  # 로그인 없이 그냥 열림

    deploy = FakeDeploy()
    with pytest.raises(pub.PublishError, match="Access"):
        pub.publish(conn, pages, now=NOW, get=open_site, deploy=deploy)
    assert deploy.calls == []
    assert pub.load_state(pages) is None  # 실패하면 다음에 다시 시도
    assert seen == [
        "https://reelbox.pages.dev/",
        "https://reelbox-access-check.reelbox.pages.dev/",
    ]


def test_access_must_cover_deployment_subdomains() -> None:
    def only_main(url: str) -> tuple[int, str | None]:
        return (302, ACCESS_LOGIN) if url == "https://reelbox.pages.dev/" else (404, None)

    problems = pub.check_access("https://reelbox.pages.dev", only_main)
    assert len(problems) == 1
    assert "reelbox-access-check" in problems[0]
    # 다른 곳으로 넘어가는 리다이렉트는 Access 가 아니다
    assert pub.check_access("https://reelbox.pages.dev", lambda u: (302, "https://evil.example/"))
    assert pub.check_access("https://reelbox.pages.dev", locked) == []


def test_without_pages_settings_only_builds(
    conn: sqlite3.Connection, library: dict[str, int], settings: Settings
) -> None:
    deploy = FakeDeploy()
    report = pub.publish(conn, settings, now=NOW, get=locked, deploy=deploy)
    assert report.result is pub.PublishResult.BUILT
    assert (settings.dist_dir / "snapshot" / "items.json").is_file()
    assert deploy.calls == []


def test_dry_run_reports_size_without_writing(
    conn: sqlite3.Connection, library: dict[str, int], pages: Settings
) -> None:
    deploy = FakeDeploy()
    report = pub.publish(conn, pages, now=NOW, dry_run=True, get=locked, deploy=deploy)
    assert report.result is pub.PublishResult.DRY_RUN
    assert report.build.files > 5
    assert report.build.bytes > 0
    assert report.build.items == 2
    assert not pages.dist_dir.exists()
    assert deploy.calls == []
