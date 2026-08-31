import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import faiss
import numpy as np
from numpy.typing import NDArray

from retrievalops.contracts import Chunk

_TERM = re.compile(r"\w+", re.UNICODE)
_DEFAULT_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


class Embedder(Protocol):
    @property
    def model_name(self) -> str: ...

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]: ...


class SentenceTransformerEmbedder:
    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        model_revision: str = _DEFAULT_MODEL_REVISION,
    ) -> None:
        self._model_name = model_name
        self._model_revision = model_revision
        self._model: Any = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_revision(self) -> str:
        return self._model_revision

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self._model_name,
                revision=self._model_revision,
            )
        encoded = self._model.encode(list(texts), normalize_embeddings=True, convert_to_numpy=True)
        return np.asarray(encoded, dtype=np.float32)


@dataclass(frozen=True, slots=True)
class RankedChunk:
    chunk_id: str
    score: float


class BM25Index:
    def __init__(self, chunk_ids: list[str], documents: list[list[str]]) -> None:
        self.chunk_ids = chunk_ids
        self.documents = documents
        self._lengths = [len(document) for document in documents]
        self._term_frequencies = [Counter(document) for document in documents]
        self._average_length = sum(self._lengths) / len(self._lengths) if documents else 0.0
        self._document_frequency = Counter(term for document in documents for term in set(document))

    @classmethod
    def build(cls, chunks: Sequence[Chunk]) -> "BM25Index":
        return cls([chunk.id for chunk in chunks], [_tokenize(chunk.text) for chunk in chunks])

    def search(self, query: str, top_k: int) -> list[RankedChunk]:
        query_terms = _tokenize(query)
        scores: list[RankedChunk] = []
        total = len(self.documents)
        for chunk_id, frequencies, length in zip(
            self.chunk_ids, self._term_frequencies, self._lengths, strict=True
        ):
            score = 0.0
            for term in query_terms:
                frequency = frequencies[term]
                if not frequency:
                    continue
                document_frequency = self._document_frequency[term]
                inverse_frequency = math.log(
                    1 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + 1.5 * (
                    1 - 0.75 + 0.75 * length / max(self._average_length, 1)
                )
                score += inverse_frequency * frequency * 2.5 / denominator
            scores.append(RankedChunk(chunk_id, score))
        return sorted(scores, key=lambda item: (-item.score, item.chunk_id))[:top_k]

    def to_bytes(self) -> bytes:
        return json.dumps(
            {"chunk_ids": self.chunk_ids, "documents": self.documents},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, content: bytes) -> "BM25Index":
        payload = json.loads(content)
        return cls(payload["chunk_ids"], payload["documents"])


class DenseIndex:
    def __init__(self, chunk_ids: list[str], index: faiss.Index) -> None:
        self.chunk_ids = chunk_ids
        self.index = index

    @classmethod
    def build(cls, chunks: Sequence[Chunk], embedder: Embedder) -> "DenseIndex":
        vectors = embedder.encode([chunk.text for chunk in chunks])
        vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        faiss.normalize_L2(vectors)
        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors)
        return cls([chunk.id for chunk in chunks], index)

    def search(self, query: str, top_k: int, embedder: Embedder) -> list[RankedChunk]:
        vector = np.ascontiguousarray(embedder.encode([query]), dtype=np.float32)
        faiss.normalize_L2(vector)
        scores, rows = self.index.search(vector, min(top_k, len(self.chunk_ids)))
        return [
            RankedChunk(self.chunk_ids[int(row)], float(score))
            for score, row in zip(scores[0], rows[0], strict=True)
            if row >= 0
        ]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RankedChunk]], top_k: int, rank_constant: int = 60
) -> list[RankedChunk]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item.chunk_id] = scores.get(item.chunk_id, 0.0) + 1 / (rank_constant + rank)
    ranked = [RankedChunk(chunk_id, score) for chunk_id, score in scores.items()]
    return sorted(ranked, key=lambda item: (-item.score, item.chunk_id))[:top_k]


def _tokenize(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _TERM.finditer(text)]
