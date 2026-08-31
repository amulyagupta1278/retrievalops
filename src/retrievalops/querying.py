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
        policy, version = self.active_policy(sandbox_id)
        executable_policy = "hybrid" if policy == "bootstrap-hybrid" else policy
        return self.search_policy(sandbox_id, query, top_k, executable_policy), version

    def active_policy(self, sandbox_id: UUID) -> tuple[str, str]:
        try:
            payload = json.loads(self._artifacts.read(f"{sandbox_id}/active_policy.json"))
            return str(payload["active_policy"]), str(payload["policy_version"])
        except FileNotFoundError:
            manifest_content = self._artifacts.read(f"{sandbox_id}/index_manifest.json")
            return "bootstrap-hybrid", hashlib.sha256(manifest_content).hexdigest()[:16]

    def search_policy(
        self, sandbox_id: UUID, query: str, top_k: int, policy: str
    ) -> list[QueryHit]:
        manifest_content = self._artifacts.read(f"{sandbox_id}/index_manifest.json")
        manifest = json.loads(manifest_content)
        for name, expected_hash in manifest["files"].items():
            content = self._artifacts.read(f"{sandbox_id}/{name}")
            if hashlib.sha256(content).hexdigest() != expected_hash:
                raise RuntimeError(f"artifact integrity failure: {name}")
        chunks_content = self._artifacts.read(f"{sandbox_id}/chunks.json")
        chunks = [Chunk.model_validate(item) for item in json.loads(chunks_content)]
        by_id = {chunk.id: chunk for chunk in chunks}
        candidate_count = min(max(top_k * 3, top_k), len(chunks))
        if policy == "bm25":
            bm25 = BM25Index.from_bytes(self._artifacts.read(f"{sandbox_id}/bm25.json"))
            ranked = bm25.search(query, top_k)
        elif policy == "dense":
            dense = self._load_dense(sandbox_id)
            ranked = dense.search(query, top_k, self._embedder)
        elif policy == "hybrid":
            bm25 = BM25Index.from_bytes(self._artifacts.read(f"{sandbox_id}/bm25.json"))
            dense = self._load_dense(sandbox_id)
            bm25_results = bm25.search(query, candidate_count)
            dense_results = dense.search(query, candidate_count, self._embedder)
            ranked = reciprocal_rank_fusion([bm25_results, dense_results], top_k)
        else:
            raise ValueError("unknown retrieval policy")
        return [QueryHit(by_id[item.chunk_id], item.score) for item in ranked]

    def _load_dense(self, sandbox_id: UUID) -> DenseIndex:
        dense_bytes = self._artifacts.read(f"{sandbox_id}/dense.faiss")
        dense_index = faiss.deserialize_index(np.frombuffer(dense_bytes, dtype=np.uint8))
        dense_ids = json.loads(self._artifacts.read(f"{sandbox_id}/dense_ids.json"))
        return DenseIndex(dense_ids, dense_index)
