"""설정. 환경변수와 프로젝트 루트의 .env 를 읽는다 (pydantic-settings).

키 이름은 대부분 REELBOX_ 접두사를 쓴다 (예: data_dir → REELBOX_DATA_DIR).
ANTHROPIC_API_KEY 만 Anthropic 관례대로 접두사 없이 읽는다.
"""

from pathlib import Path
from typing import Self

from pydantic import AliasChoices, Field, SecretStr, model_validator
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

    # 보강(enrich). 인스타 요청 간격은 5초 밑으로 내릴 수 없다 (DESIGN.md 9장).
    instagram_daily_limit: int = Field(default=200, ge=0)  # 최근 24시간 동안 처리할 최대 개수
    instagram_delay_min_sec: float = Field(default=5.0, ge=5.0)
    instagram_delay_max_sec: float = Field(default=10.0, ge=5.0)
    max_attempts: int = 3  # 실패한 항목을 다시 시도하는 최대 횟수
    # 이보다 긴 영상은 받아쓰기만 건너뛰고 제목·설명으로 분석한다 (쇼츠·릴스 최대 3분)
    max_transcribe_sec: float = 180.0

    # faster-whisper: 모델 크기 tiny/base/small/medium/large-v3, CPU 에서는 int8 이 빠르다
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    @model_validator(mode="after")
    def _check_delay(self) -> Self:
        if self.instagram_delay_max_sec < self.instagram_delay_min_sec:
            raise ValueError("instagram_delay_max_sec 는 instagram_delay_min_sec 이상이어야 함")
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "reelbox.db"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"


def get_settings() -> Settings:
    return Settings()
