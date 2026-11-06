"""yt-dlp 정보 → 메타데이터, 썸네일 저장."""

import io
from pathlib import Path

from PIL import Image

from reelbox.enrich.metadata import audio_source, parse_info, save_thumbnail, thumbnail_relpath


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buf, format="PNG")
    return buf.getvalue()


def test_youtube_info() -> None:
    info = {
        "id": "abcdefghijk",
        "title": "  김치찌개 3분 레시피 ",
        "uploader": "요리왕",
        "channel": "요리왕 채널",
        "description": "설명",
        "duration": 42,
        "formats": [{"url": "https://v"}],
        "thumbnails": [
            {"url": "https://t/small.jpg", "width": 120, "preference": -10},
            {"url": "https://t/big.jpg", "width": 1280, "preference": 0},
            {"url": "https://t/mid.jpg", "width": 480, "preference": -5},
            {"url": "https://t/nowidth.webp", "preference": 1},
        ],
    }
    meta = parse_info(info, "youtube")
    assert meta.title == "김치찌개 3분 레시피"
    assert meta.author == "요리왕"
    assert meta.description == "설명"
    assert meta.duration_sec == 42.0
    assert meta.media_type == "video"
    # 320px 이상 중 작은 것부터 → 나머지는 preference 높은 것부터
    assert meta.thumbnail_urls == [
        "https://t/mid.jpg",
        "https://t/big.jpg",
        "https://t/nowidth.webp",
        "https://t/small.jpg",
    ]
    assert audio_source(info) is info


def test_instagram_reel_drops_generated_title() -> None:
    info = {
        "id": "REEL1",
        "title": "Video by fake_chef",
        "channel": "fake_chef",
        "uploader": "가짜 요리사",
        "uploader_id": "123456",
        "description": "브라운 버터 파스타",
        "duration": 15.5,
        "formats": [{"url": "https://cdn/v.mp4"}],
        "thumbnails": [{"url": "https://cdn/t.jpg", "width": 640}],
    }
    meta = parse_info(info, "instagram")
    assert meta.title is None
    assert meta.author == "fake_chef"
    assert meta.duration_sec == 15.5
    assert meta.media_type == "video"


def test_instagram_real_title_is_kept() -> None:
    info = {"title": "진짜 제목", "channel": "fake_chef", "formats": [{"url": "u"}]}
    assert parse_info(info, "instagram").title == "진짜 제목"


def test_instagram_photo_post() -> None:
    info = {
        "title": "Video by fake_chef",
        "channel": "fake_chef",
        "description": "주말 등산 코스",
        "formats": [],
        "thumbnails": [{"url": "https://cdn/p.jpg", "width": 1080}],
    }
    meta = parse_info(info, "instagram")
    assert meta.media_type == "image"
    assert meta.description == "주말 등산 코스"
    assert meta.duration_sec is None
    assert audio_source(info) is None


def test_instagram_carousel() -> None:
    photo = {"id": "a", "formats": [], "thumbnails": [{"url": "https://cdn/1.jpg", "width": 640}]}
    video = {"id": "b", "formats": [{"url": "https://cdn/2.mp4"}], "duration": 9}
    info = {
        "_type": "playlist",
        "title": "Post by fake_chef",
        "channel": "fake_chef",
        "description": "여러 장",
        "entries": [photo, video],
    }
    meta = parse_info(info, "instagram")
    assert meta.title is None
    assert meta.media_type == "video"
    assert meta.duration_sec == 9.0
    assert meta.thumbnail_urls == ["https://cdn/1.jpg"]  # 첫 장의 썸네일
    assert audio_source(info) is video

    photos_only = {**info, "entries": [photo]}
    assert parse_info(photos_only, "instagram").media_type == "image"
    assert audio_source(photos_only) is None


def test_save_thumbnail_resizes_to_320_webp(tmp_path: Path) -> None:
    dest = tmp_path / "thumbs" / "x.webp"
    save_thumbnail(_png(640, 1136), dest)
    with Image.open(dest) as img:
        assert img.format == "WEBP"
        assert img.size == (320, 568)
    assert not dest.with_suffix(".tmp").exists()


def test_save_thumbnail_does_not_upscale(tmp_path: Path) -> None:
    dest = tmp_path / "y.webp"
    save_thumbnail(_png(200, 300), dest)
    with Image.open(dest) as img:
        assert img.size == (200, 300)


def test_thumbnail_relpath() -> None:
    assert thumbnail_relpath("instagram", "ABC") == "thumbs/instagram_ABC.webp"


def test_carousel_audio_source_inherits_extractor_from_post() -> None:
    """yt-dlp 는 넘겨받은 정보에 extractor 가 있다고 보고 읽는다 (없으면 KeyError)."""
    video = {"id": "b", "formats": [{"url": "https://cdn/2.mp4"}], "webpage_url": "https://own"}
    info = {
        "_type": "playlist",
        "extractor": "Instagram",
        "extractor_key": "Instagram",
        "webpage_url": "https://www.instagram.com/p/Dd0xeAfGAmH/",
        "entries": [video],
    }
    entry = audio_source(info)
    assert entry is video
    assert entry["extractor"] == "Instagram"
    assert entry["extractor_key"] == "Instagram"
    assert entry["webpage_url"] == "https://own"  # 이미 있는 값은 덮어쓰지 않음
