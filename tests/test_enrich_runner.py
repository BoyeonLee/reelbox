"""보강 단계 흐름. yt-dlp·whisper·sleep 은 전부 가짜라 네트워크 없이 돈다."""

import copy
import io
import random
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest
from PIL import Image
from yt_dlp.utils import DownloadError

from reelbox import db
from reelbox.config import Settings
from reelbox.enrich import runner
from reelbox.enrich.runner import EnrichDeps, classify, enrich_from_captions, enrich_items
from reelbox.enrich.transcribe import Transcript
from reelbox.ytdlp import BlockedError, CookieError
from tests.conftest import insert_item

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (640, 1136), "blue").save(buf, format="PNG")
    return buf.getvalue()


def yt_info(video_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": video_id,
        "title": f"제목 {video_id}",
        "uploader": "유튜버",
        "description": "유튜브 설명",
        "duration": 30,
        "formats": [{"url": "https://yt/v"}],
        "thumbnails": [{"url": f"https://yt/{video_id}.jpg", "width": 480}],
        **extra,
    }


def ig_info(shortcode: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": shortcode,
        "title": "Video by fake_chef",
        "channel": "fake_chef",
        "description": "인스타 캡션",
        "duration": 20,
        "formats": [{"url": "https://cdn/a.m4a"}],
        "thumbnails": [{"url": f"https://cdn/{shortcode}.jpg", "width": 640}],
        **extra,
    }


SUBS = {"subtitles": {"ko": [{"ext": "vtt", "url": "https://subs/ko.vtt"}]}}
VTT = b"WEBVTT\n\n00:00.000 --> 00:01.000\n\xec\x9e\x90\xeb\xa7\x89 \xeb\x82\xb4\xec\x9a\xa9"


class FakeClient:
    """YtdlpClient 대신. responses: URL → info dict 또는 예외."""

    def __init__(self, responses: dict[str, Any], log: list[tuple[str, str]]) -> None:
        self.responses = responses
        self.log = log
        self.audio_error: Exception | None = None
        self.audio_dirs: list[Path] = []
        self.requests = 0  # 이 클라이언트(플랫폼)로 보낸 요청 수

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.log.append(("close", ""))

    def extract(self, url: str) -> dict[str, Any]:
        self.log.append(("extract", url))
        self.requests += 1
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def fetch_bytes(self, url: str) -> bytes:
        self.log.append(("fetch", url))
        self.requests += 1
        value = self.responses.get(url)
        if isinstance(value, Exception):
            raise value
        return VTT if url.endswith(".vtt") else _png()

    def download_audio(self, info: dict[str, Any], dest_dir: Path) -> Path:
        self.log.append(("audio", info["id"]))
        self.requests += 1
        self.audio_dirs.append(dest_dir)
        path = dest_dir / "audio.m4a"
        path.write_bytes(b"audio")
        if self.audio_error is not None:
            raise self.audio_error
        return path


class FakeWhisper:
    def __init__(self, text: str = "받아쓴 말") -> None:
        self.text = text
        self.calls: list[Path] = []

    def __call__(self, audio_path: Path) -> Transcript:
        assert audio_path.exists()
        self.calls.append(audio_path)
        return Transcript(text=self.text, language="ko")


class Env:
    """테스트 하나의 가짜 외부 세계."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings
        self.log: list[tuple[str, str]] = []
        self.responses: dict[str, Any] = {}
        self.sleeps: list[float] = []
        self.whisper = FakeWhisper()
        self.clients: dict[str, FakeClient] = {}
        self.cookie_error: dict[str, CookieError] = {}

    def open_client(self, platform: str) -> FakeClient:
        if platform in self.cookie_error:
            raise self.cookie_error[platform]
        client = FakeClient(self.responses, self.log)
        self.clients[platform] = client
        return client

    def deps(self) -> EnrichDeps:
        return EnrichDeps(
            open_client=self.open_client,
            transcriber=self.whisper,
            sleep=self.sleeps.append,
            rng=random.Random(0),
            now=lambda: NOW,
        )

    def run(self, **kwargs: Any) -> runner.EnrichReport:
        return enrich_items(self.conn, self.settings, self.deps(), **kwargs)

    def yt(self, video_id: str, response: Any = None, **cols: Any) -> int:
        url = f"https://www.youtube.com/shorts/{video_id}"
        self.responses[url] = yt_info(video_id) if response is None else response
        return insert_item(self.conn, video_id, url=url, media_type="video", **cols)

    def ig(self, shortcode: str, response: Any = None, **cols: Any) -> int:
        url = f"https://www.instagram.com/reel/{shortcode}/"
        self.responses[url] = ig_info(shortcode) if response is None else response
        cols.setdefault("media_type", "video")
        return insert_item(self.conn, shortcode, platform="instagram", url=url, **cols)

    def item(self, item_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def transcript(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM transcripts WHERE item_id = ?", (item_id,)
        ).fetchone()

    def extracted(self) -> list[str]:
        return [url for kind, url in self.log if kind == "extract"]


@pytest.fixture
def env(conn: sqlite3.Connection, settings: Settings) -> Env:
    return Env(conn, settings)


# --- 성공 경로 ---


def test_youtube_with_captions(env: Env) -> None:
    item_id = env.yt("aaaaaaaaaaa", yt_info("aaaaaaaaaaa", **SUBS))
    report = env.run()

    assert report.enriched == 1
    assert report.sources == {"captions": 1}
    row = env.item(item_id)
    assert row["status"] == "enriched"
    assert row["title"] == "제목 aaaaaaaaaaa"
    assert row["author"] == "유튜버"
    assert row["description"] == "유튜브 설명"
    assert row["duration_sec"] == 30
    assert row["thumbnail_url"] == "https://yt/aaaaaaaaaaa.jpg"
    assert row["thumbnail_path"] == "thumbs/youtube_aaaaaaaaaaa.webp"
    with Image.open(env.settings.data_dir / row["thumbnail_path"]) as img:
        assert img.width == 320
    t = env.transcript(item_id)
    assert (t["source"], t["language"], t["text"]) == ("captions", "ko", "자막 내용")
    assert env.whisper.calls == []  # 자막이 있으면 받아쓰지 않는다


def test_instagram_reel_without_captions_is_transcribed_and_audio_deleted(env: Env) -> None:
    item_id = env.ig("REEL1")
    report = env.run()

    assert report.sources == {"whisper": 1}
    t = env.transcript(item_id)
    assert (t["source"], t["language"], t["text"]) == ("whisper", "ko", "받아쓴 말")
    row = env.item(item_id)
    assert row["title"] is None  # 'Video by ...' 는 버림
    assert row["author"] == "fake_chef"
    # 오디오 폴더는 data/tmp 아래에 만들어졌다가 지워졌다
    audio_dir = env.clients["instagram"].audio_dirs[0]
    assert audio_dir.parent == env.settings.tmp_dir
    assert not audio_dir.exists()
    assert list(env.settings.tmp_dir.iterdir()) == []


def test_photo_post_uses_caption_only(env: Env) -> None:
    item_id = env.ig("POST1", ig_info("POST1", formats=[], duration=None), media_type=None)
    env.run()

    row = env.item(item_id)
    assert row["media_type"] == "image"
    assert row["description"] == "인스타 캡션"
    assert env.transcript(item_id)["source"] == "none"
    assert ("audio", "POST1") not in env.log


def test_long_video_skips_transcription_but_is_enriched(env: Env) -> None:
    item_id = env.yt("bbbbbbbbbbb", yt_info("bbbbbbbbbbb", duration=600))
    env.run()

    assert env.item(item_id)["status"] == "enriched"
    assert env.transcript(item_id)["source"] == "none"
    assert not any(kind == "audio" for kind, _ in env.log)


def test_silent_video_is_none(env: Env) -> None:
    item_id = env.ig("MUTE1")
    env.whisper.text = ""  # 말소리가 없으면 whisper 가 빈 글을 돌려준다
    env.run()
    t = env.transcript(item_id)
    assert (t["source"], t["text"]) == ("none", None)


def test_export_caption_is_kept_when_ytdlp_has_none(env: Env) -> None:
    item_id = env.ig("REEL2", ig_info("REEL2", description=None), description="내보내기 캡션")
    env.run()
    assert env.item(item_id)["description"] == "내보내기 캡션"


def test_thumbnail_failure_is_not_fatal(env: Env) -> None:
    item_id = env.yt("ccccccccccc")
    env.responses["https://yt/ccccccccccc.jpg"] = OSError("썸네일 404")
    env.run()
    row = env.item(item_id)
    assert row["status"] == "enriched"
    assert row["thumbnail_path"] is None
    assert row["thumbnail_url"] == "https://yt/ccccccccccc.jpg"


def test_caption_fetch_failure_falls_back_to_whisper(env: Env) -> None:
    item_id = env.yt("ddddddddddd", yt_info("ddddddddddd", **SUBS))
    env.responses["https://subs/ko.vtt"] = OSError("자막 서버 오류")
    env.run()
    assert env.transcript(item_id)["source"] == "whisper"


# --- 실패 기록 ---


def test_unavailable(env: Env) -> None:
    item_id = env.yt(
        "eeeeeeeeeee", DownloadError("ERROR: [youtube] eeeeeeeeeee: Video unavailable")
    )
    report = env.run()

    assert report.unavailable == 1
    row = env.item(item_id)
    assert row["status"] == "unavailable"
    assert row["error"] == "[youtube] eeeeeeeeeee: Video unavailable"
    assert row["attempts"] == 0
    assert env.run().unavailable == 0  # 다시 시도하지 않는다


def test_generic_error_is_failed_and_retried_up_to_max(env: Env) -> None:
    item_id = env.yt("fffffffffff", DownloadError("ERROR: \x1b[0;31m알 수 없는 오류\x1b[0m"))
    for expected_attempts in (1, 2, 3):
        report = env.run()
        assert report.failed == 1
        row = env.item(item_id)
        assert (row["status"], row["attempts"]) == ("failed", expected_attempts)
        assert row["error"] == "알 수 없는 오류"
    assert env.run().failed == 0  # 3번 실패하면 더 시도하지 않는다
    assert env.extracted().count("https://www.youtube.com/shorts/fffffffffff") == 3


def test_failed_item_succeeds_on_retry(env: Env) -> None:
    item_id = env.yt("ggggggggggg", OSError("잠깐 끊김"))
    env.run()
    env.responses["https://www.youtube.com/shorts/ggggggggggg"] = yt_info("ggggggggggg")
    env.run()
    row = env.item(item_id)
    assert (row["status"], row["error"], row["attempts"]) == ("enriched", None, 1)


def test_failed_in_later_stage_is_not_picked(env: Env) -> None:
    item_id = env.yt("hhhhhhhhhhh", status="failed", attempts=1)
    env.conn.execute("INSERT INTO transcripts (item_id, source) VALUES (?, 'none')", (item_id,))
    env.conn.commit()
    assert env.run().enriched == 0
    assert env.extracted() == []


def test_audio_error_fails_item_and_still_deletes_audio(env: Env) -> None:
    item_id = env.ig("REEL3")
    original = env.open_client

    def open_client(platform: str) -> FakeClient:
        client = original(platform)
        client.audio_error = OSError("오디오 받다가 끊김")
        return client

    env.open_client = open_client  # type: ignore[method-assign]
    report = env.run()
    assert report.failed == 1
    assert env.item(item_id)["error"] == "오디오 받다가 끊김"
    assert not env.clients["instagram"].audio_dirs[0].exists()
    assert list(env.settings.tmp_dir.iterdir()) == []


# --- 차단·속도 제한 ---


@pytest.mark.parametrize(
    "error",
    [
        DownloadError("ERROR: [Instagram] X: HTTP Error 429: Too Many Requests"),
        DownloadError(
            "ERROR: [Instagram] X: Requested content is not available, "
            "rate-limit reached or login required"
        ),
        DownloadError("ERROR: [Instagram] X: checkpoint_required"),
        BlockedError("The provided Instagram account cookies are no longer valid"),
    ],
)
def test_block_stops_instagram_but_youtube_continues(env: Env, error: Exception) -> None:
    first = env.ig("REELa", error)
    second = env.ig("REELb")
    yt = env.yt("iiiiiiiiiii")

    report = env.run()

    assert "instagram" in report.stopped
    row = env.item(first)
    assert (row["status"], row["attempts"]) == ("failed", 1)
    assert env.item(second)["status"] == "imported"  # 손대지 않음 → 다음에 이어서
    assert "https://www.instagram.com/reel/REELb/" not in env.extracted()
    assert env.item(yt)["status"] == "enriched"


def test_blocked_while_fetching_captions_stops_too(env: Env) -> None:
    env.ig("REELc", ig_info("REELc", **SUBS))
    env.ig("REELd")
    env.responses["https://subs/ko.vtt"] = OSError("HTTP Error 429: Too Many Requests")
    report = env.run()
    assert "instagram" in report.stopped
    assert report.failed == 1


def test_instagram_waits_5_to_10_seconds_between_every_request(env: Env) -> None:
    env.ig("REELe")
    env.ig("REELf")
    env.yt("jjjjjjjjjjj")
    env.run()

    # 항목당 메타·썸네일·오디오 = 3번, 첫 요청만 바로 보낸다
    assert env.clients["instagram"].requests == 6
    assert len(env.sleeps) == 5
    assert all(5.0 <= s <= 10.0 for s in env.sleeps)
    assert len(set(env.sleeps)) == 5  # 무작위


def test_daily_limit_counts_last_24_hours(env: Env) -> None:
    env.settings.instagram_daily_limit = 3
    for hours_ago in (1, 2, 30):  # 30시간 전은 세지 않는다
        db.log_fetch(env.conn, "instagram", None, NOW - timedelta(hours=hours_ago))
    first = env.ig("REELg")
    second = env.ig("REELh")

    report = env.run()

    assert env.item(first)["status"] == "enriched"
    assert env.item(second)["status"] == "imported"
    assert "일일 한도" in report.stopped["instagram"]
    assert db.fetch_count_since(env.conn, "instagram", NOW - timedelta(hours=24)) == 3


def test_missing_instagram_cookies_stops_without_touching_items(env: Env) -> None:
    env.cookie_error["instagram"] = CookieError(cause="instagram 쿠키 설정이 없음.", fix="넣는다")
    item_id = env.ig("REELi")
    yt = env.yt("kkkkkkkkkkk")
    report = env.run()
    assert report.stopped["instagram"].startswith("instagram 쿠키 설정이 없음.")
    assert env.item(item_id)["status"] == "imported"
    assert env.item(item_id)["attempts"] == 0
    assert env.item(yt)["status"] == "enriched"


# --- 대상 고르기 ---


def test_idempotent(env: Env) -> None:
    env.yt("lllllllllll")
    assert env.run().enriched == 1
    assert env.run().enriched == 0
    assert len(env.extracted()) == 1


def test_limit_and_platform(env: Env) -> None:
    for i in range(3):
        env.yt(f"mmmmmmmmmm{i}")
        env.ig(f"REELm{i}")
    report = env.run(platform="instagram", limit=2)
    assert report.enriched == 2
    assert all("instagram" in url for url in env.extracted())
    progress: list[tuple[int, int]] = []
    env.run(limit=10, on_item=lambda n, total, item: progress.append((n, total)))
    assert progress[-1] == (4, 4)


def test_leftover_tmp_is_cleaned_at_start(env: Env) -> None:
    leftover = env.settings.tmp_dir / "audio-old"
    leftover.mkdir(parents=True)
    (leftover / "audio.m4a").write_bytes(b"x")
    assert env.run().tmp_cleaned == 1
    assert not leftover.exists()


def test_classify() -> None:
    assert classify(DownloadError("ERROR: Private video. Sign in if you've been granted")) == (
        "unavailable"
    )
    assert classify(DownloadError("Sign in to confirm you're not a bot")) == "blocked"
    assert classify(DownloadError("Unable to download JSON metadata: HTTP Error 404")) == (
        "unavailable"
    )
    assert classify(RuntimeError("뭔가 이상함")) == "error"


# --- 캡션만으로 ---


def test_captions_only_marks_items_with_caption_without_network(env: Env) -> None:
    with_caption = env.ig("REELn", description="내보내기 캡션")
    without = env.ig("REELo")
    report = enrich_from_captions(env.conn, env.settings, platform="instagram")
    assert (report.enriched, report.no_caption) == (1, 1)
    assert env.item(with_caption)["status"] == "enriched"
    assert env.transcript(with_caption)["source"] == "none"
    assert env.item(without)["status"] == "imported"
    assert env.log == []


def test_classify_instagram_audience_restriction_as_unavailable() -> None:
    message = (
        "ERROR: [Instagram] DeWgkk4SoC4: This content isn't available to everyone: "
        "It can't be seen by certain audiences."
    )
    assert classify(DownloadError(message)) == "unavailable"
