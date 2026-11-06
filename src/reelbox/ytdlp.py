"""yt-dlp 를 부르는 곳을 한데 모은다 (쿠키 설정, 로그 연결, 보강 단계용 클라이언트).

테스트에서는 YtdlpClient 대신 같은 메서드를 가진 가짜 객체를 넣는다.
"""

import logging
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import yt_dlp
from pydantic import BaseModel

from reelbox.config import PROJECT_ROOT, Settings
from reelbox.urls import Platform

logger = logging.getLogger(__name__)

_COOKIE_ENV = {"youtube": "REELBOX_YOUTUBE_COOKIES", "instagram": "REELBOX_INSTAGRAM_COOKIES"}
_COOKIE_DOMAIN = {"youtube": "youtube.com", "instagram": "instagram.com"}


class CookieError(Exception):
    """쿠키 문제로 요청을 못 함. cause=원인, fix=해결 방법, detail=yt-dlp 원문."""

    def __init__(self, *, cause: str, fix: str, detail: str = "") -> None:
        super().__init__(cause)
        self.cause = cause
        self.fix = fix
        self.detail = detail


class CookieSource(BaseModel):
    browser: str | None = None  # 'firefox' 또는 'firefox:<프로필 이름이나 경로>'
    file: Path | None = None  # Netscape 형식 cookies.txt

    @classmethod
    def from_settings(cls, settings: Settings, platform: Platform = "youtube") -> Self:
        browser = getattr(settings, f"{platform}_cookies_from_browser")
        file = getattr(settings, f"{platform}_cookies_file")
        env = _COOKIE_ENV[platform]
        if browser and file:
            raise CookieError(
                cause="쿠키 설정이 두 개 다 있음 (브라우저 이름, cookies.txt 경로). "
                "하나만 써야 함.",
                fix=f".env 에서 {env}_FROM_BROWSER 와 {env}_FILE 중 하나를 비운다.",
            )
        if not browser and not file:
            raise CookieError(
                cause=f"{platform} 쿠키 설정이 없음.",
                fix=f".env 에 {env}_FILE=secrets/{platform}_cookies.txt 또는 "
                f"{env}_FROM_BROWSER=firefox 를 넣는다.",
            )
        if file:
            if not file.is_absolute():
                file = PROJECT_ROOT / file
            if not file.is_file():
                raise CookieError(
                    cause=f"cookies.txt 파일이 없음: {file}",
                    fix="브라우저 확장(예: Get cookies.txt LOCALLY)으로 "
                    f"{_COOKIE_DOMAIN[platform]} 쿠키를 내보내 그 경로에 둔다. "
                    f"경로는 {env}_FILE.",
                )
        return cls(browser=browser, file=file)

    @staticmethod
    def is_configured(settings: Settings, platform: Platform) -> bool:
        return bool(
            getattr(settings, f"{platform}_cookies_from_browser")
            or getattr(settings, f"{platform}_cookies_file")
        )

    def ydl_opts(self) -> dict[str, Any]:
        if self.browser:
            name, _, profile = self.browser.partition(":")
            # (브라우저, 프로필, 키링, 컨테이너)
            return {"cookiesfrombrowser": (name, profile or None, None, None)}
        return {"cookiefile": str(self.file)}


class YtdlpLogger:
    """yt-dlp 출력을 logging 으로 보낸다. 오류는 예외로 따로 다루므로 debug 로 낮춘다.

    경고는 warnings 에도 모아 둔다. 인스타 쿠키 만료처럼 오류 없이 경고만 오는 신호를 잡기 위해서다.
    """

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, msg: str) -> None:
        logger.debug(msg)

    def info(self, msg: str) -> None:
        logger.debug(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)
        logger.warning(msg)

    def error(self, msg: str) -> None:
        logger.debug(msg)


class BlockedError(Exception):
    """플랫폼이 막았다는 신호 (429, 로그인 요구, 쿠키 만료 등). 그 플랫폼 처리를 멈춘다."""


# 경고만으로 오는 차단 신호. 인스타는 쿠키가 만료되면 오류 없이 비로그인으로 계속 요청한다.
_BLOCK_WARNINGS = ("cookies are no longer valid",)


def base_opts(cookies: CookieSource | None, ytdl_logger: YtdlpLogger) -> dict[str, Any]:
    """보강 단계 공통 옵션. 알아둘 옵션은 주석으로 설명한다."""
    return {
        # 영상 파일을 받지 않는다. 메타데이터만 읽는 단계라 필수.
        "skip_download": True,
        # 사진 게시물은 영상 형식이 없어서 yt-dlp 가 기본으로 'There is no video in this post'
        # 오류를 낸다. 켜 두면 오류 대신 경고만 하고 캡션·썸네일이 든 정보를 돌려준다.
        "ignore_no_formats_error": True,
        # 진행 막대·출력을 끄고, 남는 메시지는 위 logger 로 받는다.
        "quiet": True,
        "noprogress": True,
        "logger": ytdl_logger,
        # 다운로드가 끊겼을 때 다시 시도하는 횟수 (기본 10). 차단 중에 같은 요청을 여러 번
        # 보내지 않도록 줄인다. 요청 사이 대기(5~10초)는 yt-dlp 의 sleep_interval_requests 대신
        # 우리 코드(enrich.runner)가 직접 한다. 요청마다 무작위 간격을 주기 위해서다.
        "retries": 2,
        "extractor_retries": 1,
        # cookiefile 을 쓰면 yt-dlp 가 끝날 때 갱신된 쿠키를 그 파일에 다시 쓴다 (정상 동작).
        **(cookies.ydl_opts() if cookies else {}),
    }


class YtdlpClient:
    """플랫폼 하나에 대한 yt-dlp 세션. with 블록 안에서 쓴다 (끝날 때 쿠키 파일이 저장됨)."""

    def __init__(self, cookies: CookieSource | None) -> None:
        self._cookies = cookies
        self._logger = YtdlpLogger()
        self._ydl: yt_dlp.YoutubeDL | None = None

    def __enter__(self) -> Self:
        ydl = yt_dlp.YoutubeDL(base_opts(self._cookies, self._logger))
        try:
            # yt-dlp 는 쿠키를 첫 요청 때 읽는다. 여기서 미리 읽어서, 형식이 틀렸거나 브라우저
            # 쿠키를 못 읽으면 항목을 실패 처리하기 전에 바로 알 수 있게 한다.
            _ = ydl.cookiejar
        except Exception as exc:
            # ydl.close() 는 부르지 않는다. close 는 쿠키를 파일에 다시 쓰는데, 못 읽은
            # 쿠키 파일을 건드리면 안 된다. 아직 연결을 열지 않아서 닫을 것도 없다.
            raise CookieError(
                cause="쿠키를 불러오지 못함.",
                fix="cookies.txt 가 Netscape 형식인지(첫 줄 '# Netscape HTTP Cookie File') "
                "확인하고 다시 내보낸다. 브라우저 쿠키라면 cookies.txt 로 바꿔 본다.",
                detail=str(exc.__cause__ or exc),
            ) from exc
        self._ydl = ydl
        self._ydl.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._ydl is not None
        self._ydl.__exit__(exc_type, exc, tb)
        self._ydl = None

    def _check_warnings(self) -> None:
        found = [w for w in self._logger.warnings if any(s in w.lower() for s in _BLOCK_WARNINGS)]
        self._logger.warnings.clear()
        if found:
            raise BlockedError(found[0])

    def extract(self, url: str) -> dict[str, Any]:
        """다운로드 없이 정보만 읽는다.

        process=False: 형식(화질) 고르기를 하지 않은 원본 정보를 받는다. 형식 고르기에서 나는
        오류('Requested format is not available' 등) 때문에 메타데이터까지 실패하지 않게 하려는 것.
        오디오가 필요하면 download_audio 가 이 정보로 형식을 고른다 (다시 추출하지 않음).
        """
        assert self._ydl is not None
        self._logger.warnings.clear()
        info = self._ydl.extract_info(url, download=False, process=False)
        self._check_warnings()
        if info is None:
            raise RuntimeError(f"yt-dlp 가 아무 정보도 돌려주지 않음: {url}")
        return info

    def fetch_bytes(self, url: str) -> bytes:
        """썸네일·자막 파일 받기. yt-dlp 의 세션(쿠키·헤더)을 그대로 쓴다."""
        assert self._ydl is not None
        with self._ydl.urlopen(url) as resp:
            return resp.read()

    def download_audio(self, info: dict[str, Any], dest_dir: Path) -> Path:
        """extract 로 받은 정보에서 오디오만 dest_dir 에 받는다. 지우는 건 부르는 쪽 책임."""
        opts = {
            **base_opts(self._cookies, self._logger),
            "skip_download": False,
            # 오디오만 있는 형식 → 없으면 소리가 든 가장 작은 단일 파일.
            # 소리 없는 영상이면 'Requested format is not available' 오류가 난다.
            "format": "bestaudio/worst[acodec!=none]",
            # 오디오를 받을 때는 형식이 없으면 바로 오류를 내야 한다 (켜 두면 경고만 하고
            # 'No video formats found!' 로 다른 오류가 나서 '소리 없음'을 알아볼 수 없다).
            "ignore_no_formats_error": False,
            # 받는 파일 이름. %(ext)s 는 yt-dlp 가 형식에 맞는 확장자(m4a, webm 등)로 채운다.
            "outtmpl": str(dest_dir / "audio.%(ext)s"),
            # 영상+오디오 합치기·변환(ffmpeg 후처리)을 하지 않는다. faster-whisper 가
            # m4a/webm/mp4 를 직접 읽으므로 받은 파일 그대로 쓴다.
            "postprocessors": [],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            # --load-info-json 과 같은 방식: 이미 받은 정보로 형식만 고르고 내려받는다.
            ydl.process_ie_result(ydl.sanitize_info(info), download=True)
        self._check_warnings()
        files = [p for p in dest_dir.iterdir() if p.name.startswith("audio.")]
        files = [p for p in files if not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("오디오 파일을 받지 못함")
        return files[0]
