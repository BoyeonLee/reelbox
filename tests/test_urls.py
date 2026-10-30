import pytest

from reelbox.urls import parse_url


@pytest.mark.parametrize(
    ("raw", "source_id", "url", "media_type"),
    [
        # 인스타: 쿼리·조각·m.·사용자 이름 경로 제거, reels/tv → reel
        (
            "https://www.instagram.com/reel/DAbc_12-xyZ/",
            "DAbc_12-xyZ",
            "https://www.instagram.com/reel/DAbc_12-xyZ/",
            "video",
        ),
        (
            "https://www.instagram.com/reel/DAbc_12-xyZ/?igsh=MWQ1ZGUxMzBkMA==",
            "DAbc_12-xyZ",
            "https://www.instagram.com/reel/DAbc_12-xyZ/",
            "video",
        ),
        (
            "http://instagram.com/reels/DAbc_12-xyZ",
            "DAbc_12-xyZ",
            "https://www.instagram.com/reel/DAbc_12-xyZ/",
            "video",
        ),
        (
            "https://m.instagram.com/tv/DAbc_12-xyZ/#comments",
            "DAbc_12-xyZ",
            "https://www.instagram.com/reel/DAbc_12-xyZ/",
            "video",
        ),
        (
            "https://www.instagram.com/fake_chef/reel/DAbc_12-xyZ/",
            "DAbc_12-xyZ",
            "https://www.instagram.com/reel/DAbc_12-xyZ/",
            "video",
        ),
        (
            "https://www.instagram.com/p/CPost123456/?img_index=2",
            "CPost123456",
            "https://www.instagram.com/p/CPost123456/",
            None,
        ),
        (
            "www.instagram.com/p/CPost123456",
            "CPost123456",
            "https://www.instagram.com/p/CPost123456/",
            None,
        ),
        # 유튜브: youtu.be·/shorts/·watch?v=·embed·live → 동영상 ID
        (
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "video",
        ),
        (
            "https://youtube.com/shorts/dQw4w9WgXcQ?si=AbCdEf123",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "video",
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ?si=xyz&t=10",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "video",
        ),
        (
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&feature=share",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "video",
        ),
        (
            "https://music.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "video",
        ),
        (
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "video",
        ),
        (
            "https://www.youtube.com/live/dQw4w9WgXcQ?feature=share",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "video",
        ),
        (
            "  https://WWW.YOUTUBE.COM/shorts/dQw4w9WgXcQ/  ",
            "dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "video",
        ),
    ],
)
def test_parse_url_normalizes(raw: str, source_id: str, url: str, media_type: str | None) -> None:
    parsed = parse_url(raw)
    assert parsed is not None
    assert parsed.source_id == source_id
    assert parsed.url == url
    assert parsed.media_type == media_type


def test_platform() -> None:
    assert parse_url("https://youtu.be/dQw4w9WgXcQ").platform == "youtube"  # type: ignore[union-attr]
    assert parse_url("https://www.instagram.com/p/CPost123456/").platform == "instagram"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "그냥 글자",
        "https://example.com/reel/DAbc_12-xyZ/",
        "https://www.instagram.com/fake_chef/",  # 프로필
        "https://www.instagram.com/reels/",  # 릴스 탭
        "https://www.instagram.com/reels/audio/123456789/",
        "https://www.instagram.com/explore/tags/파스타/",
        "https://www.youtube.com/playlist?list=YS",
        "https://www.youtube.com/@fakechannel",
        "https://www.youtube.com/watch?v=short",  # ID 길이가 11이 아님
        "https://youtu.be/",
        "https://notyoutube.com/shorts/dQw4w9WgXcQ",
        "ftp://www.youtube.com/shorts/dQw4w9WgXcQ",
    ],
)
def test_parse_url_rejects(raw: str) -> None:
    assert parse_url(raw) is None
