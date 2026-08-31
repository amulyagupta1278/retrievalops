from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Header, Response, UploadFile, status
from pydantic import BaseModel
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from retrievalops.config import Settings, get_settings
from retrievalops.contracts import CreateSandboxResponse, Document, IngestionJob, JobState, Sandbox
from retrievalops.errors import ServiceError, service_error_handler
from retrievalops.lifecycle import SandboxLifecycle
from retrievalops.metadata import MetadataStore, create_capability_token
from retrievalops.storage import ArtifactStore
from retrievalops.uploads import validate_upload


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    build_sha: str


def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version=settings.service_version,
        build_sha=settings.build_sha,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    metadata_store = MetadataStore(runtime_settings.database_url)
    artifact_store = ArtifactStore(runtime_settings.storage_root)
    sandbox_lifecycle = SandboxLifecycle(metadata_store, artifact_store)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_settings.storage_root.mkdir(parents=True, exist_ok=True)
        metadata_store.initialize()
        application.state.metadata_store = metadata_store
        application.state.artifact_store = artifact_store
        application.state.sandbox_lifecycle = sandbox_lifecycle
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

    return application


app = create_app()
