"""자막 받기: 수동 자막(ko → en → 아무거나) → 자동 자막.

yt-dlp 정보의 subtitles(올린 사람이 넣은 자막), automatic_captions(유튜브 자동 자막)에서
트랙을 골라 받고, 시간 표시를 뺀 일반 텍스트로 바꾼다.
"""

import html
import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

# 읽을 줄 아는 형식, 앞에 있을수록 우선. json3 은 유튜브 전용이고 줄 중복이 없어서 깔끔하다.
SUPPORTED_EXTS = ("json3", "vtt")
PREFERRED_LANGS = ("ko", "en")


class CaptionTrack(BaseModel):
    language: str
    ext: str
    url: str
    automatic: bool


def base_lang(code: str) -> str:
    """'ko-KR' → 'ko', 'en-orig' → 'en'."""
    return code.split("-")[0].lower()


def _best_format(formats: Any) -> tuple[str, str] | None:
    """(ext, url). 지원하는 형식이 없으면 None."""
    if not isinstance(formats, list):
        return None
    for ext in SUPPORTED_EXTS:
        for f in formats:
            if isinstance(f, dict) and f.get("ext") == ext and f.get("url"):
                return ext, f["url"]
    return None


def _pick(tracks: dict[str, Any], order: list[str], automatic: bool) -> CaptionTrack | None:
    for lang in order:
        if (found := _best_format(tracks.get(lang))) is not None:
            ext, url = found
            return CaptionTrack(language=lang, ext=ext, url=url, automatic=automatic)
    return None


def original_language(info: dict[str, Any]) -> str | None:
    """영상의 원래 언어.

    info['language'] 는 yt-dlp 가 형식 고르기(process) 단계에서 채운다. 우리는 process=False 로
    받으므로 비어 있을 수 있다. 그때는 원래 음성 트랙을 찾는다: 유튜브는 원래 음성에
    language_preference 를 가장 높게(10) 주고, 자동 더빙 음성은 낮게(-1) 준다.
    """
    language = info.get("language")
    if isinstance(language, str) and language:
        return language
    best: tuple[int, str] | None = None
    for f in info.get("formats") or []:
        if not isinstance(f, dict):
            continue
        lang, pref = f.get("language"), f.get("language_preference")
        if isinstance(lang, str) and lang and isinstance(pref, int) and pref > 0:
            if best is None or pref > best[0]:
                best = (pref, lang)
    return best[1] if best else None


def pick_track(info: dict[str, Any]) -> CaptionTrack | None:
    """우선순위:
    1. 영상 원래 언어(info['language'])의 자막: 수동 → 자동
    2. 수동 ko → 수동 en → 수동 아무거나
    3. 자동 자막: '-orig'(원래 언어) → ko → en

    한국어 영상에 영어 번역 자막만 수동으로 있으면, 번역보다 한국어 자동 자막(원문)이 낫다.
    유튜브 자동 자막에는 원래 언어 외에 기계 번역본이 수십 개 들어 있다. 번역본 키는
    목표 언어 이름이라서, 원래 언어와 기본 언어 코드가 같은 키만 원문으로 본다.
    """
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}

    original = original_language(info)
    if original:
        lang = base_lang(original)
        same = [k for k in manual if base_lang(k) == lang]
        if (track := _pick(manual, same, automatic=False)) is not None:
            return track
        # 'ko-orig' 를 'ko' 보다 먼저 (둘 다 원문이지만 -orig 가 확실하다)
        same_auto = sorted(
            (k for k in auto if base_lang(k) == lang), key=lambda k: not k.endswith("-orig")
        )
        if (track := _pick(auto, same_auto, automatic=True)) is not None:
            return track

    order = [k for pref in PREFERRED_LANGS for k in manual if base_lang(k) == pref]
    order += [k for k in manual if k not in order]
    if (track := _pick(manual, order, automatic=False)) is not None:
        return track

    # '-orig' 가 여러 개면(자동 더빙 트랙도 '-orig' 가 붙는다) 목록 순서가 아니라 ko → en 순
    origs = [k for k in auto if k.endswith("-orig")]
    auto_order = [k for pref in PREFERRED_LANGS for k in origs if base_lang(k) == pref]
    auto_order += origs
    auto_order += [k for pref in PREFERRED_LANGS for k in auto if k == pref]
    return _pick(auto, list(dict.fromkeys(auto_order)), automatic=True)


_VTT_TIMING = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?[.,]\d{3}\s+-->")
_TAG = re.compile(r"<[^>]+>")


def _dedupe(lines: list[str]) -> str:
    """빈 줄을 빼고, 바로 앞 줄과 같은 줄을 지운다 (유튜브 자동 자막은 줄이 겹쳐 나온다)."""
    kept: list[str] = []
    for line in lines:
        line = " ".join(line.split())
        if line and (not kept or kept[-1] != line):
            kept.append(line)
    return "\n".join(kept)


def parse_vtt(text: str) -> str:
    lines: list[str] = []
    skip_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            skip_block = False
            continue
        if skip_block:
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            skip_block = True  # 빈 줄이 나올 때까지 전부 주석
            continue
        if _VTT_TIMING.match(line) or line.isdigit():
            continue
        lines.append(html.unescape(_TAG.sub("", line)))
    return _dedupe(lines)


def parse_json3(text: str) -> str:
    data = json.loads(text)
    lines: list[str] = []
    for event in data.get("events") or []:
        segs = event.get("segs") or []
        lines.append("".join(s.get("utf8", "") for s in segs if isinstance(s, dict)))
    return _dedupe(lines)


def fetch_captions(track: CaptionTrack, fetch: Callable[[str], bytes]) -> str:
    """트랙을 받아 텍스트로 바꾼다. fetch 는 YtdlpClient.fetch_bytes (테스트에서는 가짜)."""
    raw = fetch(track.url).decode("utf-8", errors="replace")
    return parse_json3(raw) if track.ext == "json3" else parse_vtt(raw)
