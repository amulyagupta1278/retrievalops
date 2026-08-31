import hashlib
import json
import time
from dataclasses import dataclass

import faiss
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from retrievalops.config import get_settings
from retrievalops.contracts import JobState
from retrievalops.metadata import MetadataStore
from retrievalops.parsing import extract_and_chunk
from retrievalops.retrieval import BM25Index, DenseIndex, Embedder, SentenceTransformerEmbedder
from retrievalops.storage import ArtifactStore
from retrievalops.telemetry import Telemetry


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    job_id: str
    state: JobState


class IngestionWorker:
    def __init__(
        self,
        metadata: MetadataStore,
        artifacts: ArtifactStore,
        embedder: Embedder,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._metadata = metadata
        self._artifacts = artifacts
        self._embedder = embedder
        self._telemetry = telemetry

    def process_next(self) -> ProcessingResult | None:
        claimed = self._metadata.claim_next_ingestion()
        if claimed is None:
            return None
        span_context = (
            self._telemetry.tracer.start_as_current_span(
                "ingestion.process",
                context=(
                    TraceContextTextMapPropagator().extract({"traceparent": claimed.traceparent})
                    if claimed.traceparent is not None
                    else None
                ),
            )
            if self._telemetry is not None
            else None
        )
        span = span_context.__enter__() if span_context is not None else None
        trace_id = f"{span.get_span_context().trace_id:032x}" if span is not None else "untraced"
        self._record_state(str(claimed.job.id), JobState.validating, trace_id)
        try:
            source = self._artifacts.read(claimed.storage_key)
            self._metadata.transition_job(claimed.job.id, JobState.validating, JobState.extracting)
            self._record_state(str(claimed.job.id), JobState.extracting, trace_id)
            extracted = extract_and_chunk(claimed.document, source)
            self._metadata.transition_job(claimed.job.id, JobState.extracting, JobState.indexing)
            self._record_state(str(claimed.job.id), JobState.indexing, trace_id)
            chunks_payload = [chunk.model_dump(mode="json") for chunk in extracted.chunks]
            chunks_key = self._artifacts.write_json(
                claimed.job.sandbox_id, "chunks.json", chunks_payload
            )
            index_started = time.perf_counter()
            bm25 = BM25Index.build(extracted.chunks)
            bm25_key = self._artifacts.write_bytes(
                claimed.job.sandbox_id, "bm25.json", bm25.to_bytes()
            )
            bm25_index_time_ms = (time.perf_counter() - index_started) * 1_000
            dense_started = time.perf_counter()
            dense = DenseIndex.build(extracted.chunks, self._embedder)
            dense_content = faiss.serialize_index(dense.index).tobytes()
            dense_key = self._artifacts.write_bytes(
                claimed.job.sandbox_id, "dense.faiss", dense_content
            )
            dense_ids_key = self._artifacts.write_json(
                claimed.job.sandbox_id, "dense_ids.json", dense.chunk_ids
            )
            dense_index_time_ms = (time.perf_counter() - dense_started) * 1_000
            files = [chunks_key, bm25_key, dense_key, dense_ids_key]
            embedder_revision = getattr(self._embedder, "model_revision", "unversioned-test-double")
            index_configuration = {
                "bm25": {"b": 0.75, "k1": 1.5},
                "chunk_overlap_tokens": 64,
                "chunk_tokens": 512,
                "dense_metric": "cosine_via_normalized_inner_product",
                "dense_model_revision": embedder_revision,
                "hybrid": {"method": "rrf", "rank_constant": 60},
            }
            configuration_bytes = json.dumps(
                index_configuration, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            manifest = {
                "schema_version": 1,
                "document_sha256": claimed.document.sha256,
                "embedder": self._embedder.model_name,
                "embedder_revision": embedder_revision,
                "embedder_identity_sha256": hashlib.sha256(
                    f"{self._embedder.model_name}@{embedder_revision}".encode()
                ).hexdigest(),
                "configuration": index_configuration,
                "configuration_sha256": hashlib.sha256(configuration_bytes).hexdigest(),
                "chunk_count": len(extracted.chunks),
                "index_time_ms": {
                    "bm25": bm25_index_time_ms,
                    "dense": dense_index_time_ms,
                    "hybrid": bm25_index_time_ms + dense_index_time_ms,
                },
                "files": {
                    key.rsplit("/", 1)[1]: hashlib.sha256(self._artifacts.read(key)).hexdigest()
                    for key in files
                },
            }
            self._artifacts.write_json(claimed.job.sandbox_id, "index_manifest.json", manifest)
            self._metadata.transition_job(claimed.job.id, JobState.indexing, JobState.ready)
            self._record_state(str(claimed.job.id), JobState.ready, trace_id)
            return ProcessingResult(str(claimed.job.id), JobState.ready)
        except Exception:
            if span is not None:
                span.set_status(Status(StatusCode.ERROR, "INGESTION_FAILED"))
            current = self._metadata.sandbox_state(claimed.job.sandbox_id)
            if current not in {None, JobState.ready, JobState.failed}:
                assert current is not None
                self._metadata.transition_job(
                    claimed.job.id, current, JobState.failed, "INGESTION_FAILED"
                )
            self._record_state(str(claimed.job.id), JobState.failed, trace_id)
            return ProcessingResult(str(claimed.job.id), JobState.failed)
        finally:
            if span_context is not None:
                span_context.__exit__(None, None, None)

    def _record_state(self, job_id: str, state: JobState, trace_id: str) -> None:
        if self._telemetry is not None:
            self._telemetry.record_job_transition(job_id, state, trace_id)


def main() -> None:
    settings = get_settings()
    metadata = MetadataStore(settings.database_url)
    metadata.initialize()
    artifacts = ArtifactStore(settings.storage_root)
    telemetry = Telemetry(
        service_name=settings.service_name,
        service_version=settings.service_version,
        otlp_traces_endpoint=settings.otlp_traces_endpoint,
    )
    worker = IngestionWorker(metadata, artifacts, SentenceTransformerEmbedder(), telemetry)
    while True:
        if worker.process_next() is None:
            time.sleep(1)


if __name__ == "__main__":
    main()
