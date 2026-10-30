"""URL 정규화: 여러 형태의 링크를 (플랫폼, 고유 ID, 정규 URL) 로 바꾼다.

같은 영상은 (platform, source_id) 로 중복을 막으므로, 모든 입구는 여기를 거친다.
"""

import re
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel

Platform = Literal["instagram", "youtube"]
MediaType = Literal["video", "image"]


class ParsedUrl(BaseModel):
    platform: Platform
    source_id: str
    url: str  # 정규 URL (쿼리·조각 제거)
    media_type: MediaType | None  # /p/ 는 사진인지 영상인지 보강 단계에서 정한다


_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am"}
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

# /p/<id>, /reel/<id>, /reels/<id>, /tv/<id>, 앞에 /<사용자 이름>/ 이 붙은 형태도 허용
_INSTAGRAM_PATH = re.compile(r"^/(?:[\w.]+/)?(p|reels?|tv)/([A-Za-z0-9_-]{5,})/?$")
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_PATH = re.compile(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]+)/?$")


def parse_url(raw: str) -> ParsedUrl | None:
    """인식할 수 있는 인스타 게시물·유튜브 동영상 링크면 ParsedUrl, 아니면 None."""
    text = raw.strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()

    if host in _INSTAGRAM_HOSTS:
        return _parse_instagram(parts.path)
    if host == "youtu.be":
        return _youtube(parts.path.strip("/"))
    if host in _YOUTUBE_HOSTS:
        if parts.path.rstrip("/") == "/watch":
            return _youtube(parse_qs(parts.query).get("v", [""])[0])
        m = _YOUTUBE_PATH.match(parts.path)
        return _youtube(m.group(1)) if m else None
    return None


def _parse_instagram(path: str) -> ParsedUrl | None:
    m = _INSTAGRAM_PATH.match(path)
    if m is None:
        return None
    kind, shortcode = m.groups()
    if kind == "reels" and shortcode == "audio":
        return None
    is_video = kind != "p"
    return ParsedUrl(
        platform="instagram",
        source_id=shortcode,
        url=f"https://www.instagram.com/{'reel' if is_video else 'p'}/{shortcode}/",
        media_type="video" if is_video else None,
    )


def _youtube(video_id: str) -> ParsedUrl | None:
    if not _YOUTUBE_ID.match(video_id):
        return None
    return ParsedUrl(
        platform="youtube",
        source_id=video_id,
        url=f"https://www.youtube.com/shorts/{video_id}",
        media_type="video",
    )
