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
    prompts_dir: Path = PROJECT_ROOT / "prompts"

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

    # AI 분석(analyze). 모델을 바꾸면 아래 가격(100만 토큰당 달러)도 같이 바꾼다.
    analyze_model: str = "claude-haiku-5-5"
    analyze_effort: str = "low"  # low/medium/high: 생각을 얼마나 할지. 요약·분류는 low 로 충분
    analyze_max_tokens: int = 4000  # 응답 하나의 최대 출력 토큰 (생각 토큰 포함)
    analyze_prompt: str = "analyze_v1"  # prompts/ 안의 파일 이름
    analyze_price_input_per_mtok: float = 0.10
    analyze_price_output_per_mtok: float = 0.50
    transcript_max_chars: int = 6000  # 자막은 앞부분만 보낸다

    # 주제 정리(organize). 주제를 짜는 일은 요약보다 판단이 많아 effort 를 한 단계 올린다.
    organize_model: str = "claude-haiku-5-5"
    organize_effort: str = "medium"
    organize_max_tokens: int = 16000  # 이보다 크면 SDK 가 스트리밍을 요구한다
    organize_prompt: str = "organize_v1"  # 대주제 만들기 + raw_topic 대응표
    organize_assign_prompt: str = "organize_assign_v1"  # 새 항목을 기존 주제에 배정
    organize_map_batch: int = Field(default=150, ge=1)  # 대응표를 한 번에 받을 raw_topic 수
    organize_assign_batch: int = Field(default=30, ge=1)  # 배정을 한 번에 부탁할 항목 수

    # 색인(index)·검색(search). bge-m3 는 처음 쓸 때 ~/.cache/huggingface 에 약 2.2GB 받는다.
    embed_model: str = "BAAI/bge-m3"
    embed_device: str = "cpu"
    embed_batch_size: int = Field(default=16, ge=1)
    embed_max_seq_length: int = 512  # 토큰. 길수록 느리다 (CPU 에서 512 면 항목당 약 1초)
    index_transcript_chars: int = 1000  # 임베딩에 넣을 자막 앞부분 글자 수
    similar_count: int = 5  # 영상마다 미리 계산해 둘 "비슷한 영상" 수

    # 공유 항목 자동 처리 워커 (serve 안에서 돈다, Phase 6)
    worker_poll_sec: float = Field(default=60.0, gt=0)  # 새 공유가 없어도 이 간격으로 할 일 확인
    # 인스타 차단·쿠키 오류나 Claude API 오류가 나면 그 단계만 이만큼 쉰다.
    # 바로 다시 두드리면 같은 항목의 시도 횟수(max_attempts)만 써 버리기 때문.
    worker_backoff_sec: float = Field(default=30 * 60.0, ge=0)

    # 보기 전용 사이트 (Phase 8, DESIGN.md 6장).
    # pages_project·pages_url 이 비어 있으면 publish 는 dist 만 만들고 배포는 안 한다.
    pages_project: str | None = None  # Cloudflare Pages 프로젝트 이름 (예: reelbox)
    pages_url: str | None = (
        None  # 휴대폰에 설치할 주소 (예: https://reelbox.pages.dev). CORS 허용 대상
    )
    pc_url: str | None = (
        None  # 스냅샷 화면이 먼저 두드려 볼 PC 주소 (https://<컴퓨터>.<tailnet>.ts.net)
    )
    pages_branch: str = (
        "main"  # Pages 프로젝트의 운영(production) 브랜치. 다르면 미리보기 주소로만 올라간다
    )
    wrangler_package: str = "wrangler@4"  # npx 로 실행할 wrangler 버전
    # wrangler 가 쓰는 Cloudflare 값. 관례대로 접두사 없이 읽어, 배포할 때 환경변수로 넘긴다.
    cloudflare_api_token: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("CLOUDFLARE_API_TOKEN", "cloudflare_api_token")
    )
    cloudflare_account_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CLOUDFLARE_ACCOUNT_ID", "cloudflare_account_id"),
    )
    publish_interval_sec: float = Field(default=24 * 60 * 60.0, gt=0)  # serve 의 자동 publish 간격

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

    @property
    def dist_dir(self) -> Path:
        return self.data_dir / "dist"

    @property
    def publish_state_path(self) -> Path:
        return self.data_dir / "publish_state.json"


def get_settings() -> Settings:
    return Settings()
