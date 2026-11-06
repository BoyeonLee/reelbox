from pathlib import Path

import pytest

from reelbox.config import PROJECT_ROOT, Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ANTHROPIC_API_KEY", "REELBOX_TOKEN", "REELBOX_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)
    assert s.data_dir == PROJECT_ROOT / "data"
    assert s.db_path == PROJECT_ROOT / "data" / "reelbox.db"
    assert (s.migrations_dir / "001_init.sql").is_file()
    assert s.anthropic_api_key is None
    assert s.token is None


def test_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REELBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("REELBOX_TOKEN", "tok-test")
    monkeypatch.setenv("REELBOX_YOUTUBE_COOKIES_FROM_BROWSER", "firefox")
    s = Settings(_env_file=None)
    assert s.db_path == tmp_path / "reelbox.db"
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-test"
    assert s.token is not None
    assert s.token.get_secret_value() == "tok-test"
    assert s.youtube_cookies_from_browser == "firefox"


def test_secrets_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-very-secret")
    monkeypatch.setenv("REELBOX_TOKEN", "tok-very-secret")
    s = Settings(_env_file=None)
    assert "very-secret" not in repr(s)
    assert "very-secret" not in str(s.model_dump())


def test_instagram_delay_cannot_go_below_5_seconds() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, instagram_delay_min_sec=1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, instagram_delay_min_sec=8, instagram_delay_max_sec=6)
    s = Settings(_env_file=None)
    assert (s.instagram_delay_min_sec, s.instagram_delay_max_sec) == (5.0, 10.0)
    assert s.instagram_daily_limit == 200
    assert s.whisper_model == "small"
