"""유튜브 '저장된 Shorts 동영상' 목록 동기화.

yt-dlp --flat-playlist 와 같은 방식(extract_flat)으로 영상을 받지 않고 ID 목록만 읽는다.
내 계정의 비공개 목록이라 로그인 쿠키가 필요하다. 쿠키를 못 읽으면 우회하지 않고
원인과 해결 방법을 담은 CookieError 를 올린다.
"""

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

import yt_dlp
from pydantic import BaseModel

from reelbox import db
from reelbox.importers import ImportReport

# CookieError·CookieSource 는 보강 단계와 같이 쓰려고 reelbox.ytdlp 로 옮겼다 (여기서 다시 내보냄)
from reelbox.ytdlp import CookieError, CookieSource, YtdlpLogger

logger = logging.getLogger(__name__)

SOURCE = "youtube_sync"


class PlaylistError(Exception):
    """쿠키 외의 이유로 목록을 못 읽음 (네트워크, 유튜브 쪽 오류 등)."""


class PlaylistEntry(BaseModel):
    video_id: str
    title: str | None = None


def _extract(url: str, opts: dict[str, Any]) -> dict[str, Any]:
    """yt-dlp 를 실제로 부르는 유일한 곳. 테스트에서는 이 함수를 가짜로 바꾼다.

    주의: cookiefile 을 쓰면 yt-dlp 가 끝날 때 갱신된 쿠키를 그 파일에 다시 쓴다.
    """
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise PlaylistError(f"yt-dlp 가 아무 정보도 돌려주지 않음: {url}")
    return info


_BROWSER_COOKIE_SIGNS = (
    "cookies database",
    "decrypt",
    "could not copy",
    "keyring",
    "unsupported browser",
    "secretstorage",
)
_LOGIN_SIGNS = (
    "playlist does not exist",
    "sign in",
    "login required",
    "this playlist is private",
    "unviewable",
    "cookies are no longer valid",
)


def _classify(exc: Exception) -> Exception:
    detail = str(exc)
    text = detail.lower()
    if "netscape" in text:
        return CookieError(
            cause="cookies.txt 형식이 아님 (Netscape 형식이어야 함).",
            fix="파일 첫 줄이 '# Netscape HTTP Cookie File' 인지 확인하고, "
            "Get cookies.txt LOCALLY 같은 확장으로 다시 내보낸다.",
            detail=detail,
        )
    if any(sign in text for sign in _BROWSER_COOKIE_SIGNS):
        return CookieError(
            cause="브라우저 쿠키를 읽지 못함. WSL 의 yt-dlp 는 리눅스 쪽 브라우저 폴더만 보고, "
            "윈도우 Chrome 쿠키는 암호화돼 있어서 읽지 못하는 경우가 많다.",
            fix="(1) 쿠키를 cookies.txt 로 내보내 REELBOX_YOUTUBE_COOKIES_FILE 로 쓴다 (권장). "
            "(2) 윈도우 Firefox 를 쓰려면 프로필 경로를 지정한다: "
            "REELBOX_YOUTUBE_COOKIES_FROM_BROWSER=firefox:/mnt/c/Users/<사용자>/AppData/"
            "Roaming/Mozilla/Firefox/Profiles/<프로필>",
            detail=detail,
        )
    if any(sign in text for sign in _LOGIN_SIGNS):
        return CookieError(
            cause="로그인되지 않은 상태로 목록을 열었음. 쿠키가 만료·교체됐거나 "
            "다른 계정의 쿠키일 수 있다 ('저장된 Shorts 동영상'은 내 계정에서만 보인다).",
            fix="PC 브라우저에서 같은 계정으로 목록 주소가 열리는지 확인한 뒤 "
            "쿠키를 다시 내보낸다. "
            "유튜브는 열려 있는 탭의 쿠키를 자주 바꾸므로, 시크릿 창에서 로그인 → 쿠키 내보내기 → "
            "창 닫기 순서가 오래간다.",
            detail=detail,
        )
    return PlaylistError(detail)


def fetch_playlist(url: str, cookies: CookieSource, limit: int | None) -> list[PlaylistEntry]:
    """목록의 영상 ID 를 목록 순서대로 읽는다. limit 이 있으면 앞에서부터 그만큼만."""
    opts: dict[str, Any] = {
        "extract_flat": "in_playlist",  # = --flat-playlist
        "skip_download": True,
        "quiet": True,
        "logger": YtdlpLogger(),
        **cookies.ydl_opts(),
    }
    if limit is not None:
        opts["playlistend"] = limit
    try:
        info = _extract(url, opts)
    except (CookieError, PlaylistError):
        raise
    except Exception as exc:  # yt-dlp 는 쿠키 오류를 여러 종류의 예외로 올린다
        raise _classify(exc) from exc

    entries: list[PlaylistEntry] = []
    for entry in info.get("entries") or []:
        if not entry or not isinstance(entry.get("id"), str):
            logger.warning("ID 가 없는 목록 항목을 건너뜀: %s", entry)
            continue
        entries.append(PlaylistEntry(video_id=entry["id"], title=entry.get("title")))
    return entries[:limit] if limit is not None else entries


def add_entries(
    conn: sqlite3.Connection, entries: list[PlaylistEntry], *, now: datetime | None = None
) -> ImportReport:
    """목록에는 저장 시각이 없어서 처음 발견한 시각을 saved_at 에 넣는다.

    목록은 최신순이라 뒤(오래된 것)부터 넣는다. 그러면 id 순서가 저장 순서가 된다.
    이미 있는 영상은 건너뛰므로 다시 돌리면 새로 저장한 것만 추가된다.
    """
    saved_at = now or datetime.now(UTC)
    report = ImportReport()
    with conn:
        for entry in reversed(entries):
            url = f"https://www.youtube.com/shorts/{entry.video_id}"
            report.record(db.add_item(conn, url, saved_at=saved_at, source=SOURCE))
    return report
