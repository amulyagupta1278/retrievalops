import hashlib
import json
import time
from dataclasses import dataclass

import faiss

from retrievalops.config import get_settings
from retrievalops.contracts import JobState
from retrievalops.metadata import MetadataStore
from retrievalops.parsing import extract_and_chunk
from retrievalops.retrieval import BM25Index, DenseIndex, Embedder, SentenceTransformerEmbedder
from retrievalops.storage import ArtifactStore


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    job_id: str
    state: JobState


class IngestionWorker:
    def __init__(
        self, metadata: MetadataStore, artifacts: ArtifactStore, embedder: Embedder
    ) -> None:
        self._metadata = metadata
        self._artifacts = artifacts
        self._embedder = embedder

    def process_next(self) -> ProcessingResult | None:
        claimed = self._metadata.claim_next_ingestion()
        if claimed is None:
            return None
        try:
            source = self._artifacts.read(claimed.storage_key)
            self._metadata.transition_job(claimed.job.id, JobState.validating, JobState.extracting)
            extracted = extract_and_chunk(claimed.document, source)
            self._metadata.transition_job(claimed.job.id, JobState.extracting, JobState.indexing)
            chunks_payload = [chunk.model_dump(mode="json") for chunk in extracted.chunks]
            chunks_key = self._artifacts.write_json(
                claimed.job.sandbox_id, "chunks.json", chunks_payload
            )
            bm25 = BM25Index.build(extracted.chunks)
            bm25_key = self._artifacts.write_bytes(
                claimed.job.sandbox_id, "bm25.json", bm25.to_bytes()
            )
            dense = DenseIndex.build(extracted.chunks, self._embedder)
            dense_content = faiss.serialize_index(dense.index).tobytes()
            dense_key = self._artifacts.write_bytes(
                claimed.job.sandbox_id, "dense.faiss", dense_content
            )
            dense_ids_key = self._artifacts.write_json(
                claimed.job.sandbox_id, "dense_ids.json", dense.chunk_ids
            )
            files = [chunks_key, bm25_key, dense_key, dense_ids_key]
            index_configuration = {
                "bm25": {"b": 0.75, "k1": 1.5},
                "chunk_overlap_tokens": 64,
                "chunk_tokens": 512,
                "dense_metric": "cosine_via_normalized_inner_product",
                "hybrid": {"method": "rrf", "rank_constant": 60},
            }
            configuration_bytes = json.dumps(
                index_configuration, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            manifest = {
                "schema_version": 1,
                "document_sha256": claimed.document.sha256,
                "embedder": self._embedder.model_name,
                "embedder_identity_sha256": hashlib.sha256(
                    self._embedder.model_name.encode("utf-8")
                ).hexdigest(),
                "configuration": index_configuration,
                "configuration_sha256": hashlib.sha256(configuration_bytes).hexdigest(),
                "chunk_count": len(extracted.chunks),
                "files": {
                    key.rsplit("/", 1)[1]: hashlib.sha256(self._artifacts.read(key)).hexdigest()
                    for key in files
                },
            }
            self._artifacts.write_json(claimed.job.sandbox_id, "index_manifest.json", manifest)
            self._metadata.transition_job(claimed.job.id, JobState.indexing, JobState.ready)
            return ProcessingResult(str(claimed.job.id), JobState.ready)
        except Exception:
            current = self._metadata.sandbox_state(claimed.job.sandbox_id)
            if current not in {None, JobState.ready, JobState.failed}:
                assert current is not None
                self._metadata.transition_job(
                    claimed.job.id, current, JobState.failed, "INGESTION_FAILED"
                )
            return ProcessingResult(str(claimed.job.id), JobState.failed)


def main() -> None:
    settings = get_settings()
    metadata = MetadataStore(settings.database_url)
    metadata.initialize()
    artifacts = ArtifactStore(settings.storage_root)
    worker = IngestionWorker(metadata, artifacts, SentenceTransformerEmbedder())
    while True:
        if worker.process_next() is None:
            time.sleep(1)


if __name__ == "__main__":
    main()
