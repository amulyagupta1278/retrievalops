import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

import faiss
import numpy as np

from retrievalops.contracts import Chunk
from retrievalops.retrieval import (
    BM25Index,
    DenseIndex,
    Embedder,
    RankedChunk,
    reciprocal_rank_fusion,
)
from retrievalops.storage import ArtifactStore


@dataclass(frozen=True, slots=True)
class QueryHit:
    chunk: Chunk
    score: float


class QueryService:
    def __init__(self, artifacts: ArtifactStore, embedder: Embedder) -> None:
        self._artifacts = artifacts
        self._embedder = embedder

    def search(self, sandbox_id: UUID, query: str, top_k: int) -> tuple[list[QueryHit], str]:
        manifest_content = self._artifacts.read(f"{sandbox_id}/index_manifest.json")
        manifest = json.loads(manifest_content)
        for name, expected_hash in manifest["files"].items():
            content = self._artifacts.read(f"{sandbox_id}/{name}")
            if hashlib.sha256(content).hexdigest() != expected_hash:
                raise RuntimeError(f"artifact integrity failure: {name}")
        chunks_content = self._artifacts.read(f"{sandbox_id}/chunks.json")
        chunks = [Chunk.model_validate(item) for item in json.loads(chunks_content)]
        by_id = {chunk.id: chunk for chunk in chunks}
        bm25 = BM25Index.from_bytes(self._artifacts.read(f"{sandbox_id}/bm25.json"))
        dense_bytes = self._artifacts.read(f"{sandbox_id}/dense.faiss")
        dense_index = faiss.deserialize_index(np.frombuffer(dense_bytes, dtype=np.uint8))
        dense_ids = json.loads(self._artifacts.read(f"{sandbox_id}/dense_ids.json"))
        dense = DenseIndex(dense_ids, dense_index)
        candidate_count = min(max(top_k * 3, top_k), len(chunks))
        bm25_results = bm25.search(query, candidate_count)
        dense_results = dense.search(query, candidate_count, self._embedder)
        hybrid: list[RankedChunk] = reciprocal_rank_fusion([bm25_results, dense_results], top_k)
        version = hashlib.sha256(manifest_content).hexdigest()[:16]
        return [QueryHit(by_id[item.chunk_id], item.score) for item in hybrid], version
