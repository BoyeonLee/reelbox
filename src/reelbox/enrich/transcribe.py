"""자막이 없을 때 오디오만 임시로 받아 faster-whisper로 받아쓴다.

오디오는 data/tmp/audio-XXXX/ 에 받고, 받아쓰기가 성공하든 예외가 나든 finally 에서 지운다.
프로그램이 강제로 꺼져서 남은 폴더는 다음 enrich 시작 때 clean_tmp 가 지운다.
"""

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

logger = logging.getLogger(__name__)

TMP_PREFIX = "audio-"


class Transcript(BaseModel):
    text: str
    language: str | None


class Transcriber(Protocol):
    """오디오 파일 → 받아쓰기. 실제로는 WhisperTranscriber, 테스트에서는 가짜."""

    def __call__(self, audio_path: Path) -> Transcript: ...


class AudioDownloader(Protocol):
    def __call__(self, info: dict[str, Any], dest_dir: Path) -> Path: ...


class NoAudioError(Exception):
    """소리가 든 형식이 없음 (소리 없는 영상)."""


class WhisperTranscriber:
    """faster-whisper 모델은 처음 쓸 때 한 번만 불러온다 (첫 실행 땐 모델 파일을 내려받음)."""

    def __init__(self, model_size: str, device: str, compute_type: str) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model: Any = None

    def __call__(self, audio_path: Path) -> Transcript:
        if self._model is None:
            # 무거운 라이브러리라 실제로 받아쓸 때만 불러온다
            from faster_whisper import WhisperModel

            logger.info("whisper 모델 불러오는 중: %s", self._model_size)
            self._model = WhisperModel(
                self._model_size, device=self._device, compute_type=self._compute_type
            )
        # language 를 주지 않으면 앞부분으로 언어를 자동 감지한다.
        # vad_filter: 말소리가 없는 구간(음악만 나오는 부분)을 건너뛴다. 음악만 있는 영상에서
        # 없는 말을 지어내는(환각) 것을 줄인다.
        segments, info = self._model.transcribe(str(audio_path), vad_filter=True, beam_size=5)
        # segments 는 제너레이터라서 여기서 끝까지 돌 때 실제 받아쓰기가 일어난다
        text = "\n".join(s.text.strip() for s in segments if s.text.strip())
        return Transcript(text=text, language=info.language)


def transcribe_audio(
    info: dict[str, Any],
    tmp_root: Path,
    download: AudioDownloader,
    transcriber: Transcriber,
) -> Transcript:
    """오디오를 tmp_root 아래 임시 폴더에 받아 받아쓰고, 무슨 일이 있어도 폴더를 지운다."""
    tmp_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=TMP_PREFIX, dir=tmp_root))
    try:
        try:
            audio = download(info, work_dir)
        except Exception as exc:
            if "requested format is not available" in str(exc).lower():
                raise NoAudioError(str(exc)) from exc
            raise
        return transcriber(audio)
    finally:
        _remove(work_dir)


def _remove(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("임시 오디오 폴더를 지우지 못함: %s", path)


def clean_tmp(tmp_root: Path) -> int:
    """지난 실행에서 남은 임시 오디오 폴더를 지운다. 지운 개수를 돌려준다."""
    if not tmp_root.is_dir():
        return 0
    leftovers = [p for p in tmp_root.iterdir() if p.name.startswith(TMP_PREFIX)]
    for path in leftovers:
        if path.is_dir():
            _remove(path)
        else:
            path.unlink(missing_ok=True)
    if leftovers:
        logger.warning("지난 실행에서 남은 임시 오디오 %d개를 지움", len(leftovers))
    return len(leftovers)
