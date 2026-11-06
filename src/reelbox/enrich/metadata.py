"""yt-dlp로 다운로드 없이 받은 정보(info dict)에서 메타데이터를 뽑고 썸네일을 저장한다.

info 는 YtdlpClient.extract 가 돌려준 것이다. 인스타 여러 장 게시물(캐러셀)은
_type == 'playlist' 이고 entries 에 사진·영상이 하나씩 들어 있다.
"""

import io
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import BaseModel

from reelbox.urls import MediaType, Platform

THUMB_WIDTH = 320
THUMB_QUALITY = 80


class Metadata(BaseModel):
    title: str | None
    author: str | None
    description: str | None
    duration_sec: float | None
    media_type: MediaType
    thumbnail_urls: list[str]  # 앞에 있을수록 우선해서 시도


def _str(value: Any) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _is_video(entry: dict[str, Any]) -> bool:
    """영상 형식이 하나라도 있으면 영상. 사진은 formats 가 비어 있다."""
    return bool(entry.get("formats")) or bool(entry.get("url"))


def _entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in info.get("entries") or [] if isinstance(e, dict)]


def audio_source(info: dict[str, Any]) -> dict[str, Any] | None:
    """받아쓰기할 오디오가 있는 정보. 캐러셀이면 첫 번째 영상, 사진만 있으면 None."""
    if info.get("_type") == "playlist":
        return next((e for e in _entries(info) if _is_video(e)), None)
    return info if _is_video(info) else None


def _thumbnail_urls(info: dict[str, Any]) -> list[str]:
    """320px 이상 중 가장 작은 것 → 나머지는 큰 것부터. 너무 큰 원본을 받지 않으려는 것."""
    thumbs = [t for t in info.get("thumbnails") or [] if isinstance(t, dict) and t.get("url")]
    wide = sorted((t for t in thumbs if (t.get("width") or 0) >= THUMB_WIDTH), key=_width)
    rest = sorted(
        (t for t in thumbs if (t.get("width") or 0) < THUMB_WIDTH),
        key=lambda t: (t.get("preference") or 0, _width(t)),
        reverse=True,
    )
    urls = [info["thumbnail"]] if isinstance(info.get("thumbnail"), str) else []
    urls += [t["url"] for t in wide + rest]
    return list(dict.fromkeys(urls))


def _width(thumb: dict[str, Any]) -> int:
    return thumb.get("width") or 0


def parse_info(info: dict[str, Any], platform: Platform) -> Metadata:
    entries = _entries(info) if info.get("_type") == "playlist" else []
    audio = audio_source(info)
    first = entries[0] if entries else info

    if platform == "instagram":
        # 인스타 아이디는 channel, 표시 이름은 uploader (uploader_id 는 숫자 ID)
        author = _str(info.get("channel")) or _str(info.get("uploader"))
        # 인스타엔 제목이 없어서 yt-dlp 가 'Video by 아이디' 를 넣는다. 검색에 방해라 버린다.
        title = _str(info.get("title"))
        if title and author and title in (f"Video by {author}", f"Post by {author}"):
            title = None
    else:
        author = _str(info.get("uploader")) or _str(info.get("channel"))
        title = _str(info.get("title"))
    if author:
        author = author.removeprefix("@")

    duration = (audio or {}).get("duration")
    return Metadata(
        title=title,
        author=author,
        description=_str(info.get("description")) or _str(first.get("description")),
        duration_sec=float(duration) if isinstance(duration, int | float) else None,
        media_type="video" if audio is not None else "image",
        thumbnail_urls=_thumbnail_urls(info) or _thumbnail_urls(first),
    )


def save_thumbnail(data: bytes, dest: Path) -> None:
    """가로 320px webp 로 줄여 저장한다 (작으면 키우지 않음)."""
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        if img.width > THUMB_WIDTH:
            height = round(img.height * THUMB_WIDTH / img.width)
            img = img.resize((THUMB_WIDTH, height), Image.Resampling.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        img.save(tmp, format="WEBP", quality=THUMB_QUALITY)
    tmp.replace(dest)  # 다 쓴 뒤 이름을 바꿔서, 중간에 끊겨도 깨진 파일이 남지 않게


def thumbnail_relpath(platform: str, source_id: str) -> str:
    """DB 에 넣는 경로 (data_dir 기준)."""
    return f"thumbs/{platform}_{source_id}.webp"
