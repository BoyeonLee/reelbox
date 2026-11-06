"""모델을 교체할 수 있는 LLM 인터페이스와 Anthropic 구현.

analyze 는 LLM 프로토콜에만 기대므로, 다른 모델로 바꾸려면 같은 메서드를 가진 클래스를
하나 더 만들면 된다. 테스트에서는 가짜 LLM 을 넣는다.
"""

import logging
from typing import Protocol, TypeVar

import anthropic
from pydantic import BaseModel, SecretStr, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0  # 생각(thinking) 토큰도 출력 토큰으로 과금된다

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class StructuredResult[T: BaseModel](BaseModel):
    """검증된 출력과 사용량. (class X[T] 는 Go 의 제네릭 타입 파라미터와 같은 3.12 문법)"""

    output: T
    usage: Usage


# --- 오류: analyze 는 종류에 따라 다르게 처리한다 ---


class LLMError(Exception):
    """항목 하나만 실패로 기록하고 다음 항목으로 넘어간다."""


class LLMOutputError(LLMError):
    """응답이 형식에 맞지 않음 (JSON 검증 실패, 길이 초과). 한 번 다시 시도한다."""


class LLMRefusal(LLMError):
    """모델이 안전 정책으로 거절함. 다시 시도해도 같아서 바로 실패로 기록한다."""


class LLMFatalError(Exception):
    """API 키·모델 이름·잔액 문제처럼 모든 항목이 똑같이 실패할 오류. 전체를 멈춘다."""


class LLMUnavailableError(Exception):
    """속도 제한·과부하·네트워크 문제가 SDK 재시도 뒤에도 계속됨. 전체를 멈추고 나중에 다시."""


class LLM(Protocol):
    model: str

    def structured(self, *, system: str, user: str, schema: type[T]) -> StructuredResult[T]:
        """system·user 를 보내 schema 모양의 검증된 객체를 받는다."""
        ...

    def count_tokens(self, *, system: str, user: str, schema: type[BaseModel]) -> int:
        """같은 요청의 입력 토큰 수 (무료 API, 모델을 부르지 않음)."""
        ...


class AnthropicLLM:
    """Claude API. 구조화 출력(output_config.format)으로 JSON 모양을 강제한다."""

    def __init__(
        self,
        model: str,
        *,
        effort: str,
        max_tokens: int,
        api_key: SecretStr | None = None,
    ) -> None:
        self.model = model
        self._effort = effort
        self._max_tokens = max_tokens
        # api_key 가 없으면 SDK 가 ANTHROPIC_API_KEY 환경변수 등에서 알아서 찾는다.
        # 429·5xx·연결 오류는 SDK 가 지수 백오프로 자동 재시도한다 (기본 2회 → 4회로 늘림).
        self._client = anthropic.Anthropic(
            api_key=api_key.get_secret_value() if api_key else None, max_retries=4
        )

    def _output_config(self, schema: type[BaseModel]) -> dict[str, object]:
        # transform_schema: Pydantic 모델을 API 가 받는 JSON 스키마로 바꾼다. API 가 지원하지
        # 않는 제약(최대 길이 등)은 설명으로 옮겨지므로, 실제 검증은 받은 뒤 Pydantic 이 한다.
        return {
            "effort": self._effort,
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
        }

    def structured(self, *, system: str, user: str, schema: type[T]) -> StructuredResult[T]:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config=self._output_config(schema),  # type: ignore[arg-type]
            )
        except (
            anthropic.AuthenticationError,  # 401: 키가 틀림
            anthropic.PermissionDeniedError,  # 403: 권한 없음
            anthropic.NotFoundError,  # 404: 모델 이름이 틀림
            anthropic.BadRequestError,  # 400: 잔액 부족·잘못된 설정 (모든 항목이 같음)
        ) as exc:
            raise LLMFatalError(_api_error_text(exc)) from exc
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            raise LLMUnavailableError(_api_error_text(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailableError(_api_error_text(exc)) from exc
            raise LLMError(_api_error_text(exc)) from exc

        usage = Usage(
            input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens
        )
        # content 를 읽기 전에 stop_reason 부터 본다
        if response.stop_reason == "refusal":
            details = response.stop_details
            reason = (details.category or details.explanation) if details else None
            raise LLMRefusal(f"모델이 거절함 ({reason or '이유 없음'})")
        if response.stop_reason == "max_tokens":
            raise LLMOutputError(f"출력이 max_tokens({self._max_tokens})에서 잘림")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            output = schema.model_validate_json(text)
        except ValidationError as exc:
            raise LLMOutputError(f"출력 검증 실패: {exc.errors()[:3]}") from exc
        return StructuredResult(output=output, usage=usage)

    def count_tokens(self, *, system: str, user: str, schema: type[BaseModel]) -> int:
        try:
            result = self._client.messages.count_tokens(
                model=self.model,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config=self._output_config(schema),  # type: ignore[arg-type]
            )
        except (anthropic.AuthenticationError, anthropic.NotFoundError) as exc:
            raise LLMFatalError(_api_error_text(exc)) from exc
        except anthropic.APIError as exc:
            raise LLMUnavailableError(_api_error_text(exc)) from exc
        return result.input_tokens


def _api_error_text(exc: anthropic.APIError) -> str:
    status = getattr(exc, "status_code", None)
    prefix = f"Claude API {status}" if status else "Claude API 연결 오류"
    return f"{prefix}: {exc.message}"
