"""설정. 환경변수와 프로젝트 루트의 .env 를 읽는다 (pydantic-settings).

키 이름은 대부분 REELBOX_ 접두사를 쓴다 (예: data_dir → REELBOX_DATA_DIR).
ANTHROPIC_API_KEY 만 Anthropic 관례대로 접두사 없이 읽는다.
"""

from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/reelbox/config.py → 저장소 루트
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REELBOX_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # .env 에 'KEY=' 로 비워 둔 값은 설정 안 함(None)으로 본다
        env_ignore_empty=True,
    )

    data_dir: Path = PROJECT_ROOT / "data"
    migrations_dir: Path = PROJECT_ROOT / "migrations"

    # 비밀값: SecretStr 은 출력·로그에 '**********' 로 가려진다
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "anthropic_api_key")
    )
    token: SecretStr | None = None  # REELBOX_TOKEN

    # 쿠키: 브라우저 이름(예: firefox) 또는 cookies.txt 경로 중 하나
    youtube_cookies_from_browser: str | None = None
    youtube_cookies_file: Path | None = None
    instagram_cookies_from_browser: str | None = None
    instagram_cookies_file: Path | None = None

    youtube_saved_shorts_url: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "reelbox.db"


def get_settings() -> Settings:
    return Settings()
