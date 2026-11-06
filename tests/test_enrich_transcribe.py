"""받아쓰기: 임시 오디오는 성공하든 실패하든 지워져야 한다."""

from pathlib import Path
from typing import Any

import pytest

from reelbox.enrich.transcribe import NoAudioError, Transcript, clean_tmp, transcribe_audio


def _download(info: dict[str, Any], dest_dir: Path) -> Path:
    path = dest_dir / "audio.m4a"
    path.write_bytes(b"fake audio")
    (dest_dir / "audio.m4a.part").write_bytes(b"leftover")
    return path


def test_success_removes_tmp(tmp_path: Path) -> None:
    seen: list[Path] = []

    def transcriber(audio: Path) -> Transcript:
        assert audio.read_bytes() == b"fake audio"  # 받아쓰는 동안에는 파일이 있다
        seen.append(audio)
        return Transcript(text="안녕하세요", language="ko")

    result = transcribe_audio({"id": "x"}, tmp_path / "tmp", _download, transcriber)
    assert result == Transcript(text="안녕하세요", language="ko")
    assert seen and not seen[0].exists()
    assert list((tmp_path / "tmp").iterdir()) == []


def test_transcriber_error_still_removes_tmp(tmp_path: Path) -> None:
    def transcriber(audio: Path) -> Transcript:
        raise RuntimeError("모델 오류")

    with pytest.raises(RuntimeError, match="모델 오류"):
        transcribe_audio({"id": "x"}, tmp_path, _download, transcriber)
    assert list(tmp_path.iterdir()) == []


def test_download_error_still_removes_tmp(tmp_path: Path) -> None:
    def download(info: dict[str, Any], dest_dir: Path) -> Path:
        (dest_dir / "audio.webm.part").write_bytes(b"half")
        raise OSError("연결 끊김")

    with pytest.raises(OSError, match="연결 끊김"):
        transcribe_audio(
            {"id": "x"}, tmp_path, download, lambda p: Transcript(text="", language=None)
        )
    assert list(tmp_path.iterdir()) == []


def test_no_audio_format(tmp_path: Path) -> None:
    def download(info: dict[str, Any], dest_dir: Path) -> Path:
        raise RuntimeError("ERROR: [instagram] x: Requested format is not available")

    with pytest.raises(NoAudioError):
        transcribe_audio(
            {"id": "x"}, tmp_path, download, lambda p: Transcript(text="", language=None)
        )
    assert list(tmp_path.iterdir()) == []


def test_clean_tmp_removes_only_audio_leftovers(tmp_path: Path) -> None:
    (tmp_path / "audio-abc").mkdir()
    (tmp_path / "audio-abc" / "audio.m4a").write_bytes(b"x")
    (tmp_path / "audio-stray.m4a").write_bytes(b"x")
    (tmp_path / "keep.txt").write_text("남겨 둠")
    assert clean_tmp(tmp_path) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]
    assert clean_tmp(tmp_path / "missing") == 0
