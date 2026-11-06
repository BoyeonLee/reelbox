"""공용 yt-dlp 래퍼: 플랫폼별 쿠키 설정."""

from pathlib import Path

import pytest

from reelbox.config import Settings
from reelbox.ytdlp import CookieError, CookieSource, YtdlpClient


def test_instagram_cookie_settings(tmp_path: Path) -> None:
    with pytest.raises(CookieError) as e:
        CookieSource.from_settings(Settings(_env_file=None), "instagram")
    assert "REELBOX_INSTAGRAM_COOKIES" in e.value.fix

    cookie = tmp_path / "ig.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n")
    s = Settings(_env_file=None, instagram_cookies_file=cookie)
    assert CookieSource.from_settings(s, "instagram").ydl_opts() == {"cookiefile": str(cookie)}
    assert CookieSource.is_configured(s, "instagram")
    assert not CookieSource.is_configured(s, "youtube")


def test_bad_cookie_file_becomes_cookie_error(tmp_path: Path) -> None:
    cookie = tmp_path / "bad.txt"
    cookie.write_text("이건 쿠키 파일이 아님\n")
    with pytest.raises(CookieError) as e, YtdlpClient(CookieSource(file=cookie)):
        pass
    assert "Netscape" in e.value.fix
