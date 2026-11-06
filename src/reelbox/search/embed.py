"""bge-m3 임베딩 생성.

임베딩: 글을 1024개 숫자로 된 벡터(좌표)로 바꾼 것. 뜻이 비슷한 글은 좌표도 가까워서,
"찌개 끓이는 법"으로 검색해도 "김치찌개 레시피" 영상을 찾을 수 있다.
벡터는 길이 1로 정규화하므로 두 벡터의 내적(dot product)이 곧 코사인 유사도다.

index·organize·search 는 Embedder 프로토콜에만 기대고, 테스트에서는 가짜를 넣는다.
"""

import logging
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from reelbox.db import EmbedInput

logger = logging.getLogger(__name__)

Matrix = npt.NDArray[np.float32]  # (개수, 차원) 모양의 2차원 배열


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> Matrix:
        """texts 각각을 길이 1로 정규화한 벡터로. 결과는 (len(texts), 1024) 배열."""
        ...


class BgeM3Embedder:
    """sentence-transformers 로 bge-m3 를 돌린다. 모델(약 2.2GB)은 처음 encode 할 때 불러온다.

    불러오는 데 CPU 에서 30초쯤 걸리므로, 할 일이 없으면 아예 불러오지 않게 미룬다.
    """

    def __init__(self, model: str, *, device: str, batch_size: int, max_seq_length: int) -> None:
        self._model_name = model
        self._device = device
        self._batch_size = batch_size
        self._max_seq_length = max_seq_length
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            # torch 를 import 하는 데만 몇 초 걸려서, 실제로 필요할 때 import 한다
            from sentence_transformers import SentenceTransformer
            from transformers.utils import logging as hf_logging

            hf_logging.disable_progress_bar()  # "Loading weights" 진행 막대 숨김
            logger.info("임베딩 모델 불러오는 중: %s", self._model_name)
            self._model = SentenceTransformer(self._model_name, device=self._device)
            self._model.max_seq_length = self._max_seq_length
        return self._model

    def encode(self, texts: list[str]) -> Matrix:
        if not texts:
            return np.zeros((0, 1024), dtype=np.float32)
        vectors = self._load().encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return np.asarray(vectors, dtype=np.float32)


def item_text(item: EmbedInput, transcript_chars: int) -> str:
    """임베딩할 텍스트: 제목 + 요약 + 태그 + 자막 앞부분 (DESIGN.md 5-5)."""
    parts = [item.title or "", item.summary_ko or ""]
    if item.tags:
        parts.append("태그: " + ", ".join(item.tags))
    if item.transcript_text:
        parts.append(item.transcript_text.strip()[:transcript_chars])
    return "\n".join(p for p in parts if p)


def encode_batched(
    embedder: Embedder,
    texts: list[str],
    batch_size: int,
    on_progress: Callable[[int, int], None] | None = None,
) -> Matrix:
    """진행 상황을 알릴 수 있게 batch_size 씩 나눠 인코딩한다. on_progress(끝난 수, 전체)."""
    chunks: list[Matrix] = []
    for start in range(0, len(texts), batch_size):
        chunks.append(embedder.encode(texts[start : start + batch_size]))
        if on_progress is not None:
            on_progress(min(start + batch_size, len(texts)), len(texts))
    if not chunks:
        return np.zeros((0, 1024), dtype=np.float32)
    return np.vstack(chunks)
