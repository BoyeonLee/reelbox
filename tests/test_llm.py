"""AnthropicLLM: 응답·오류를 우리 오류 종류로 바꾸는지. 실제 API 는 부르지 않는다."""

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, SecretStr

from reelbox.ai.llm import (
    AnthropicLLM,
    LLMError,
    LLMFatalError,
    LLMOutputError,
    LLMRefusal,
    LLMUnavailableError,
    Usage,
)


class Out(BaseModel):
    answer: str


def _response(text: str = '{"answer": "네"}', stop_reason: str = "end_turn", **extra: Any) -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop_reason,
        stop_details=extra.get("stop_details"),
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def count_tokens(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return SimpleNamespace(input_tokens=1234)


def _llm(result: Any) -> tuple[AnthropicLLM, FakeMessages]:
    llm = AnthropicLLM("claude-haiku-5-5", effort="low", max_tokens=4000, api_key=SecretStr("k"))
    messages = FakeMessages(result)
    llm._client = SimpleNamespace(messages=messages)  # type: ignore[assignment]
    return llm, messages


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("문제", response=httpx2.Response(status, request=request), body=None)


def test_structured_success_sends_schema_and_effort() -> None:
    llm, messages = _llm(_response())
    result = llm.structured(system="시스템", user="내용", schema=Out)
    assert result.output == Out(answer="네")
    assert result.usage == Usage(input_tokens=100, output_tokens=20)
    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["system"] == "시스템"
    assert call["messages"] == [{"role": "user", "content": "내용"}]
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"]["required"] == ["answer"]


def test_refusal_is_checked_before_parsing() -> None:
    details = SimpleNamespace(category="cyber", explanation=None)
    llm, _ = _llm(_response(text="죄송합니다", stop_reason="refusal", stop_details=details))
    with pytest.raises(LLMRefusal, match="cyber"):
        llm.structured(system="s", user="u", schema=Out)


@pytest.mark.parametrize(
    "response",
    [
        _response(text="JSON 아님"),
        _response(text='{"wrong": 1}'),
        _response(stop_reason="max_tokens"),
    ],
)
def test_bad_output_is_output_error(response: Any) -> None:
    llm, _ = _llm(response)
    with pytest.raises(LLMOutputError):
        llm.structured(system="s", user="u", schema=Out)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_status_error(anthropic.AuthenticationError, 401), LLMFatalError),
        (_status_error(anthropic.NotFoundError, 404), LLMFatalError),
        (_status_error(anthropic.BadRequestError, 400), LLMFatalError),
        (_status_error(anthropic.RateLimitError, 429), LLMUnavailableError),
        (_status_error(anthropic.InternalServerError, 529), LLMUnavailableError),
        (
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            LLMUnavailableError,
        ),
        (_status_error(anthropic.ConflictError, 409), LLMError),
    ],
)
def test_api_errors_are_mapped(error: Exception, expected: type[Exception]) -> None:
    llm, _ = _llm(error)
    with pytest.raises(expected) as e:
        llm.structured(system="s", user="u", schema=Out)
    assert type(e.value) is expected


def test_count_tokens() -> None:
    llm, messages = _llm(None)
    assert llm.count_tokens(system="s", user="u", schema=Out) == 1234
    assert "max_tokens" not in messages.calls[0]
