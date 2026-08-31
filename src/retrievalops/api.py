from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Header, Response, UploadFile, status
from pydantic import BaseModel, Field
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from retrievalops.config import Settings, get_settings
from retrievalops.contracts import CreateSandboxResponse, Document, IngestionJob, JobState, Sandbox
from retrievalops.errors import ServiceError, service_error_handler
from retrievalops.lifecycle import SandboxLifecycle
from retrievalops.metadata import MetadataStore, create_capability_token
from retrievalops.querying import QueryService
from retrievalops.retrieval import Embedder, SentenceTransformerEmbedder
from retrievalops.storage import ArtifactStore
from retrievalops.uploads import validate_upload
from retrievalops.worker import IngestionWorker


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    build_sha: str


class JobResponse(BaseModel):
    id: UUID
    sandbox_id: UUID
    state: JobState
    error_code: str | None


class QueryRequest(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=1_000)]
    top_k: Annotated[int, Field(ge=1, le=20)] = 10


class QueryResult(BaseModel):
    chunk_id: str
    text: str
    score: float
    rank: int


class QueryResponse(BaseModel):
    trace_id: UUID
    policy: str
    policy_version: str
    latency_ms: float
    results: list[QueryResult]


def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version=settings.service_version,
        build_sha=settings.build_sha,
    )


def create_app(settings: Settings | None = None, embedder: Embedder | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    metadata_store = MetadataStore(runtime_settings.database_url)
    artifact_store = ArtifactStore(runtime_settings.storage_root)
    sandbox_lifecycle = SandboxLifecycle(metadata_store, artifact_store)
    runtime_embedder = embedder or SentenceTransformerEmbedder()
    ingestion_worker = IngestionWorker(metadata_store, artifact_store, runtime_embedder)
    query_service = QueryService(artifact_store, runtime_embedder)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_settings.storage_root.mkdir(parents=True, exist_ok=True)
        metadata_store.initialize()
        application.state.metadata_store = metadata_store
        application.state.artifact_store = artifact_store
        application.state.sandbox_lifecycle = sandbox_lifecycle
        application.state.ingestion_worker = ingestion_worker
        yield

    application = FastAPI(
        title="RetrievalOps",
        version=runtime_settings.service_version,
        description="Evidence-driven retrieval policy release control.",
        lifespan=lifespan,
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_size=runtime_settings.max_upload_bytes + 64 * 1024,
    )
    application.add_exception_handler(ServiceError, service_error_handler)
    application.get("/healthz", response_model=HealthResponse, tags=["operations"])(health)

    @application.post(
        "/v1/sandboxes",
        response_model=CreateSandboxResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["sandboxes"],
    )
    async def create_sandbox(
        file: Annotated[UploadFile, File(description="One PDF, TXT, or Markdown document")],
    ) -> CreateSandboxResponse:
        validated = await validate_upload(
            file,
            max_bytes=runtime_settings.max_upload_bytes,
            max_pdf_pages=runtime_settings.max_pdf_pages,
        )
        now = datetime.now(UTC)
        sandbox = Sandbox(
            id=uuid4(),
            created_at=now,
            expires_at=now + timedelta(hours=runtime_settings.sandbox_ttl_hours),
        )
        document = Document(
            id=uuid4(),
            sandbox_id=sandbox.id,
            filename=validated.filename,
            media_type=validated.media_type,
            size_bytes=len(validated.content),
            sha256=validated.sha256,
        )
        job = IngestionJob(id=uuid4(), sandbox_id=sandbox.id, state=JobState.queued)
        token = create_capability_token()
        storage_key = artifact_store.write_source(sandbox.id, validated.content)
        try:
            metadata_store.create_upload(sandbox, document, job, token, storage_key)
        except Exception:
            artifact_store.delete_sandbox(sandbox.id)
            raise
        return CreateSandboxResponse(
            sandbox_id=sandbox.id,
            sandbox_token=token,
            ingestion_job_id=job.id,
            expires_at=sandbox.expires_at,
        )

    @application.delete(
        "/v1/sandboxes/{sandbox_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["sandboxes"],
    )
    def delete_sandbox(
        sandbox_id: UUID,
        sandbox_token: Annotated[str, Header(alias="X-Sandbox-Token", min_length=1)],
    ) -> Response:
        if not metadata_store.token_matches(sandbox_id, sandbox_token):
            raise ServiceError(404, "SANDBOX_NOT_FOUND", "The sandbox was not found.")
        sandbox_lifecycle.delete(
            sandbox_id,
            now=datetime.now(UTC),
            reason="USER_REQUEST",
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/v1/jobs/{job_id}", response_model=JobResponse, tags=["ingestion"])
    def get_job(
        job_id: UUID,
        sandbox_token: Annotated[str, Header(alias="X-Sandbox-Token", min_length=1)],
    ) -> JobResponse:
        job = metadata_store.get_job(job_id)
        if job is None or not metadata_store.token_matches(job.sandbox_id, sandbox_token):
            raise ServiceError(404, "JOB_NOT_FOUND", "The ingestion job was not found.")
        return JobResponse(**job.model_dump())

    @application.post(
        "/v1/sandboxes/{sandbox_id}/query",
        response_model=QueryResponse,
        tags=["retrieval"],
    )
    def query_sandbox(
        sandbox_id: UUID,
        request: QueryRequest,
        sandbox_token: Annotated[str, Header(alias="X-Sandbox-Token", min_length=1)],
    ) -> QueryResponse:
        if not metadata_store.token_matches(sandbox_id, sandbox_token):
            raise ServiceError(404, "SANDBOX_NOT_FOUND", "The sandbox was not found.")
        if metadata_store.sandbox_state(sandbox_id) != JobState.ready:
            raise ServiceError(409, "SANDBOX_NOT_READY", "Sandbox ingestion is not complete.")
        started = perf_counter()
        try:
            hits, policy_version = query_service.search(sandbox_id, request.query, request.top_k)
        except (OSError, ValueError, RuntimeError):
            raise ServiceError(
                503, "RETRIEVAL_UNAVAILABLE", "The retrieval index is unavailable."
            ) from None
        elapsed_ms = (perf_counter() - started) * 1_000
        return QueryResponse(
            trace_id=uuid4(),
            policy="bootstrap-hybrid-rrf",
            policy_version=policy_version,
            latency_ms=elapsed_ms,
            results=[
                QueryResult(
                    chunk_id=hit.chunk.id,
                    text=hit.chunk.text,
                    score=hit.score,
                    rank=rank,
                )
                for rank, hit in enumerate(hits, start=1)
            ],
        )

    return application


app = create_app()
