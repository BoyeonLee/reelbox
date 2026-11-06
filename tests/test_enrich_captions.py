"""자막 트랙 고르기와 vtt/json3 → 텍스트 변환."""

import json

from reelbox.enrich.captions import (
    CaptionTrack,
    fetch_captions,
    original_language,
    parse_json3,
    parse_vtt,
    pick_track,
)


def _fmt(*exts: str, lang: str = "x") -> list[dict[str, str]]:
    return [{"ext": e, "url": f"https://subs/{lang}.{e}"} for e in exts]


def test_manual_korean_first() -> None:
    info = {
        "subtitles": {"en": _fmt("vtt", lang="en"), "ko": _fmt("vtt", lang="ko")},
        "automatic_captions": {"ko": _fmt("json3", lang="ako")},
    }
    track = pick_track(info)
    assert track == CaptionTrack(
        language="ko", ext="vtt", url="https://subs/ko.vtt", automatic=False
    )


def test_manual_regional_code_and_english() -> None:
    assert pick_track({"subtitles": {"ko-KR": _fmt("vtt")}}).language == "ko-KR"
    info = {"subtitles": {"ja": _fmt("vtt"), "en-US": _fmt("vtt")}}
    assert pick_track(info).language == "en-US"


def test_manual_any_language_before_automatic() -> None:
    info = {
        "subtitles": {"ja": _fmt("vtt", lang="ja"), "live_chat": _fmt("json3")},
        "automatic_captions": {"ko": _fmt("vtt")},
    }
    track = pick_track(info)
    assert track.language == "ja"
    assert not track.automatic


def test_automatic_prefers_original_language_over_translation() -> None:
    # 자동 자막 목록에는 기계 번역(ko 등)이 들어 있다. 원래 언어(-orig)가 먼저.
    info = {
        "language": "en",
        "automatic_captions": {
            "ko": _fmt("vtt", lang="ko"),
            "en-orig": _fmt("json3", "vtt", lang="en-orig"),
            "en": _fmt("vtt", lang="en"),
        },
    }
    track = pick_track(info)
    assert track.language == "en-orig"
    assert track.ext == "json3"  # json3 이 vtt 보다 우선
    assert track.automatic


def test_automatic_uses_video_language() -> None:
    info = {"language": "ja", "automatic_captions": {"ko": _fmt("vtt"), "ja": _fmt("vtt")}}
    assert pick_track(info).language == "ja"


def test_original_language_auto_beats_translated_manual() -> None:
    # 한국어 영상에 영어 수동 자막(번역)만 있으면 한국어 자동 자막(원문)을 고른다
    info = {
        "language": "ko",
        "subtitles": {"en": _fmt("vtt", lang="en")},
        "automatic_captions": {"en": _fmt("vtt", lang="aen"), "ko": _fmt("vtt", lang="ako")},
    }
    track = pick_track(info)
    assert (track.language, track.automatic) == ("ko", True)


def test_original_language_manual_first() -> None:
    info = {
        "language": "ko",
        "subtitles": {"en": _fmt("vtt"), "ko-KR": _fmt("vtt", lang="ko")},
        "automatic_captions": {"ko-orig": _fmt("vtt"), "ko": _fmt("vtt")},
    }
    track = pick_track(info)
    assert (track.language, track.automatic) == ("ko-KR", False)


def test_original_language_prefers_orig_key() -> None:
    info = {"language": "ko", "automatic_captions": {"ko": _fmt("vtt"), "ko-orig": _fmt("vtt")}}
    assert pick_track(info).language == "ko-orig"


def test_original_language_without_captions_falls_back() -> None:
    # 원래 언어 자막이 하나도 없으면 예전 순서 (수동 ko → en → 아무거나 → 자동)
    info = {"language": "ja", "subtitles": {"en": _fmt("vtt")}, "automatic_captions": {}}
    assert pick_track(info).language == "en"


# 실제 유튜브 정보(process=False)의 모양: language 가 비어 있고, 원래 음성(ko)과 자동 더빙
# 음성(en-US)이 함께 있으며 자동 자막에 'en-US-orig' 와 'ko-orig' 가 둘 다 있다.
DUBBED = {
    "language": None,
    "formats": [
        {"language": "en-US", "language_preference": -1, "acodec": "opus"},
        {"language": "ko", "language_preference": 10, "acodec": "opus"},
        {"acodec": "none"},
    ],
    "automatic_captions": {
        "en": _fmt("vtt"),
        "ko": _fmt("vtt"),
        "en-US-orig": _fmt("vtt"),
        "en-US": _fmt("vtt"),
        "ko-orig": _fmt("vtt"),
    },
}


def test_original_language_from_audio_track() -> None:
    assert original_language(DUBBED) == "ko"
    assert original_language({"language": "ja", **{"formats": DUBBED["formats"]}}) == "ja"
    assert original_language({"formats": [{"language": "en", "language_preference": -1}]}) is None
    assert original_language({}) is None


def test_auto_dubbed_video_uses_original_korean() -> None:
    assert pick_track(DUBBED).language == "ko-orig"


def test_several_orig_tracks_without_language_prefers_korean() -> None:
    info = {**DUBBED, "formats": []}
    assert pick_track(info).language == "ko-orig"


def test_unsupported_format_and_nothing() -> None:
    assert pick_track({"subtitles": {"ko": [{"ext": "srv1", "url": "u"}]}}) is None
    assert pick_track({}) is None
    # 자동 자막은 원래 언어·ko·en 이 아니면 쓰지 않는다 (아무 번역본이나 고르지 않음)
    assert pick_track({"automatic_captions": {"de": _fmt("vtt")}}) is None


def test_parse_vtt_removes_timing_tags_and_duplicates() -> None:
    vtt = """WEBVTT
Kind: captions
Language: ko

NOTE 이건 주석
여러 줄 주석

1
00:00:00.000 --> 00:00:02.000 align:start position:0%
안녕하세요<00:00:00.500><c> 여러분</c>

2
00:00:02.000 --> 00:00:04.000
안녕하세요 여러분
오늘은 &amp; 레시피

00:00:04.000 --> 00:00:06.000
오늘은 &amp; 레시피
"""
    assert parse_vtt(vtt) == "안녕하세요 여러분\n오늘은 & 레시피"


def test_parse_json3() -> None:
    data = {
        "events": [
            {"tStartMs": 0, "segs": [{"utf8": "hello"}, {"utf8": " world"}]},
            {"tStartMs": 100},
            {"tStartMs": 200, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 300, "segs": [{"utf8": "bye"}]},
        ]
    }
    assert parse_json3(json.dumps(data)) == "hello world\nbye"


def test_fetch_captions_uses_given_fetch() -> None:
    track = CaptionTrack(language="en", ext="vtt", url="https://subs/en.vtt", automatic=False)
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return b"WEBVTT\n\n00:00.000 --> 00:01.000\nhi"

    assert fetch_captions(track, fetch) == "hi"
    assert seen == ["https://subs/en.vtt"]
