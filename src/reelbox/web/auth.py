"""REELBOX_TOKEN 확인 (DESIGN.md 6장 "접근 제한").

Tailscale 이 1차 방어선이고, 이건 2차 방어선이다. 토큰은 둘 중 하나로 받는다.
- 쿠키 reelbox_token: 휴대폰 화면. 처음 한 번 토큰을 붙여넣으면 서버가 쿠키로 심는다.
- 헤더 Authorization: Bearer <토큰>: 아이폰 단축어 등 프로그램 (Phase 6).
"""

import secrets

from fastapi import HTTPException, Request, status

from reelbox.config import Settings

COOKIE_NAME = "reelbox_token"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1년. 홈 화면 앱에서 매번 로그인하지 않게


def token_matches(settings: Settings, candidate: str | None) -> bool:
    if settings.token is None or not candidate:
        return False
    # compare_digest: 글자를 하나씩 비교하다 일찍 끝나지 않아서, 응답 시간으로 토큰을 추측할 수 없다
    return secrets.compare_digest(
        candidate.encode("utf-8"), settings.token.get_secret_value().encode("utf-8")
    )


def request_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return request.cookies.get(COOKIE_NAME)


def require_token(request: Request) -> None:
    """FastAPI 의존성(Depends). 토큰이 맞지 않으면 401."""
    settings: Settings = request.app.state.settings
    if not token_matches(settings, request_token(request)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="토큰이 필요합니다")


def is_https(request: Request) -> bool:
    """tailscale serve 뒤에서는 서버까지는 http 로 오므로 X-Forwarded-Proto 도 본다."""
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.split(",")[0].strip() == "https"
