from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID, uuid4, uuid5

from fastapi import FastAPI, File, Header, Request, Response, UploadFile, status
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, Field
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from retrievalops.config import Settings, get_settings
from retrievalops.contracts import (
    CreateSandboxResponse,
    Document,
    EvaluationSuggestion,
    Feedback,
    IngestionJob,
    JobState,
    Judgment,
    PolicyDecision,
    Sandbox,
)
from retrievalops.errors import ServiceError, service_error_handler
from retrievalops.evaluation import EvaluationService
from retrievalops.learning import FeedbackGovernance, RetrainingWorkflow
from retrievalops.lifecycle import SandboxLifecycle
from retrievalops.lineage import LineageRegistry, ephemeral_lineage
from retrievalops.metadata import MetadataStore, create_capability_token
from retrievalops.querying import QueryService
from retrievalops.release import PolicyReleaseController
from retrievalops.retrieval import Embedder, SentenceTransformerEmbedder
from retrievalops.storage import ArtifactStore
from retrievalops.telemetry import Telemetry
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


class JudgmentInput(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=1_000)]
    relevant_chunk_id: Annotated[str, Field(min_length=1)]
    relevance: Annotated[int, Field(ge=0, le=3)] = 3
    reviewed: bool


class JudgmentsRequest(BaseModel):
    judgments: Annotated[list[JudgmentInput], Field(min_length=1, max_length=5)]


class JudgmentsResponse(BaseModel):
    stored: int
    reviewed: int
    optimization_unlocked: bool


class FeedbackRequest(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=1_000)]
    relevant_chunk_id: Annotated[str, Field(min_length=1, max_length=128)]
    relevance: Annotated[int, Field(ge=0, le=3)]


class FeedbackReceipt(BaseModel):
    id: UUID
    status: Literal["pending"]
    submitted_at: datetime


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
    telemetry = Telemetry(
        service_name=runtime_settings.service_name,
        service_version=runtime_settings.service_version,
        otlp_traces_endpoint=runtime_settings.otlp_traces_endpoint,
    )
    ingestion_worker = IngestionWorker(metadata_store, artifact_store, runtime_embedder, telemetry)
    query_service = QueryService(artifact_store, runtime_embedder)
    lineage_registry = (
        LineageRegistry(runtime_settings.mlflow_tracking_uri, runtime_settings.mlflow_artifact_root)
        if runtime_settings.mlflow_tracking_uri
        else None
    )

    def register_ephemeral(
        sandbox_id: UUID, decision: PolicyDecision, *, alias: Literal["champion", "candidate"]
    ) -> None:
        if lineage_registry is None:
            return
        lineage_registry.register(
            ephemeral_lineage(
                decision,
                sandbox_id=str(sandbox_id),
                commit_sha=runtime_settings.build_sha,
                dependency_lock_hash=runtime_settings.dependency_lock_hash,
            ),
            alias=alias,
        )

    def register_champion(sandbox_id: UUID, decision: PolicyDecision) -> None:
        register_ephemeral(sandbox_id, decision, alias="champion")

    def register_candidate(sandbox_id: UUID, decision: PolicyDecision) -> None:
        register_ephemeral(sandbox_id, decision, alias="candidate")

    def promote_candidate(sandbox_id: UUID) -> None:
        if lineage_registry is not None:
            lineage_registry.promote_ephemeral_candidate(str(sandbox_id))

    evaluation_service = EvaluationService(
        artifact_store,
        query_service,
        registrar=register_champion if lineage_registry is not None else None,
    )
    policy_release_controller = PolicyReleaseController(
        artifact_store,
        telemetry,
        promoter=promote_candidate if lineage_registry is not None else None,
    )
    feedback_governance = FeedbackGovernance(metadata_store)
    retraining_workflow = RetrainingWorkflow(
        metadata_store,
        artifact_store,
        evaluation_service,
        telemetry,
        minimum_approved_feedback=runtime_settings.drift_min_approved_feedback,
        query_drift_threshold=runtime_settings.query_drift_threshold,
        registrar=register_candidate if lineage_registry is not None else None,
        releaser=policy_release_controller.start,
    )
    feedback_governance.bind_retraining(retraining_workflow.run)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_settings.storage_root.mkdir(parents=True, exist_ok=True)
        metadata_store.initialize()
        application.state.metadata_store = metadata_store
        application.state.artifact_store = artifact_store
        application.state.sandbox_lifecycle = sandbox_lifecycle
        application.state.ingestion_worker = ingestion_worker
        application.state.lineage_registry = lineage_registry
        application.state.feedback_governance = feedback_governance
        application.state.retraining_workflow = retraining_workflow
        application.state.policy_release_controller = policy_release_controller
        try:
            yield
        finally:
            telemetry.tracer_provider.shutdown()

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
    telemetry.install(application)
    application.add_exception_handler(ServiceError, service_error_handler)
    application.get("/healthz", response_model=HealthResponse, tags=["operations"])(health)

    @application.post(
        "/v1/sandboxes",
        response_model=CreateSandboxResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["sandboxes"],
    )
    async def create_sandbox(
        request: Request,
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
        trace_carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(trace_carrier)
        try:
            metadata_store.create_upload(
                sandbox,
                document,
                job,
                token,
                storage_key,
                traceparent=trace_carrier.get("traceparent"),
            )
        except Exception:
            artifact_store.delete_sandbox(sandbox.id)
            raise
        telemetry.record_job_transition(str(job.id), JobState.queued, request.state.trace_id)
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
        http_request: Request,
        sandbox_token: Annotated[str, Header(alias="X-Sandbox-Token", min_length=1)],
    ) -> QueryResponse:
        if not metadata_store.token_matches(sandbox_id, sandbox_token):
            raise ServiceError(404, "SANDBOX_NOT_FOUND", "The sandbox was not found.")
        if metadata_store.sandbox_state(sandbox_id) != JobState.ready:
            raise ServiceError(409, "SANDBOX_NOT_READY", "Sandbox ingestion is not complete.")
        started = perf_counter()
        trace_id = UUID(hex=http_request.state.trace_id)
        active_policy, _ = query_service.active_policy(sandbox_id)
        try:
            hits, served_policy, policy_version = policy_release_controller.search(
                query_service,
                sandbox_id,
                trace_id,
                request.query,
                request.top_k,
            )
        except (OSError, ValueError, RuntimeError):
            telemetry.record_retrieval(active_policy, "unavailable")
            raise ServiceError(
                503, "RETRIEVAL_UNAVAILABLE", "The retrieval index is unavailable."
            ) from None
        elapsed_ms = (perf_counter() - started) * 1_000
        telemetry.record_retrieval(served_policy, "success")
        if served_policy == "bootstrap-hybrid":
            telemetry.record_fallback("bootstrap_policy")
        return QueryResponse(
            trace_id=trace_id,
            policy=served_policy,
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

    def require_ready_sandbox(sandbox_id: UUID, sandbox_token: str) -> None:
        if not metadata_store.token_matches(sandbox_id, sandbox_token):
            raise ServiceError(404, "SANDBOX_NOT_FOUND", "The sandbox was not found.")
        if metadata_store.sandbox_state(sandbox_id) != JobState.ready:
            raise ServiceError(409, "SANDBOX_NOT_READY", "Sandbox ingestion is not complete.")

    @application.get(
        "/v1/sandboxes/{sandbox_id}/evaluation-suggestions",
        response_model=list[EvaluationSuggestion],
        tags=["evaluation"],
    )
    def evaluation_suggestions(
        sandbox_id: UUID,
        sandbox_token: Annotated[str, Header(alias="X-Sandbox-Token", min_length=1)],
    ) -> list[EvaluationSuggestion]:
        require_ready_sandbox(sandbox_id, sandbox_token)
        return evaluation_service.suggestions(sandbox_id)

    @application.put(
        "/v1/sandboxes/{sandbox_id}/judgments",
        response_model=JudgmentsResponse,
        tags=["evaluation"],
    )
    def replace_judgments(
        sandbox_id: UUID,
        request: JudgmentsRequest,
        sandbox_token: Annotated[str, Header(alias="X-Sandbox-Token", min_length=1)],
    ) -> JudgmentsResponse:
        require_ready_sandbox(sandbox_id, sandbox_token)
        valid_chunk_ids = {chunk.id for chunk in evaluation_service.chunks(sandbox_id)}
        judgments: list[Judgment] = []
        for ordinal, item in enumerate(request.judgments):
            if item.relevant_chunk_id not in valid_chunk_ids:
                raise ServiceError(
                    422,
                    "INVALID_RELEVANT_CHUNK",
                    "A relevant passage does not belong to this sandbox.",
                )
            judgments.append(
                Judgment(
                    id=uuid5(sandbox_id, f"judgment:{ordinal}:{item.query}"),
                    sandbox_id=sandbox_id,
                    **item.model_dump(),
                )
            )
        metadata_store.replace_judgments(sandbox_id, judgments)
        reviewed = sum(judgment.reviewed and judgment.relevance > 0 for judgment in judgments)
        return JudgmentsResponse(
            stored=len(judgments),
            reviewed=reviewed,
            optimization_unlocked=reviewed >= 3,
        )

    @application.post(
        "/v1/sandboxes/{sandbox_id}/optimize",
        response_model=PolicyDecision,
        tags=["evaluation"],
    )
    def optimize(
        sandbox_id: UUID,
        sandbox_token: Annotated[str, Header(alias="X-Sandbox-Token", min_length=1)],
    ) -> PolicyDecision:
        require_ready_sandbox(sandbox_id, sandbox_token)
        judgments = [
            judgment
            for judgment in metadata_store.judgments(sandbox_id, reviewed_only=True)
            if judgment.relevance > 0
        ]
        if len(judgments) < 3:
            raise ServiceError(
                409,
                "INSUFFICIENT_REVIEWED_JUDGMENTS",
                "Confirm at least three judgments before optimization.",
            )
        return evaluation_service.optimize(sandbox_id, judgments)

    @application.get(
        "/v1/sandboxes/{sandbox_id}/policy",
        response_model=PolicyDecision,
        tags=["evaluation"],
    )
    def get_policy(
        sandbox_id: UUID,
        sandbox_token: Annotated[str, Header(alias="X-Sandbox-Token", min_length=1)],
    ) -> PolicyDecision:
        require_ready_sandbox(sandbox_id, sandbox_token)
        try:
            return PolicyDecision.model_validate_json(
                artifact_store.read(f"{sandbox_id}/active_policy.json")
            )
        except FileNotFoundError:
            raise ServiceError(
                404, "POLICY_NOT_OPTIMIZED", "No optimized policy exists for this sandbox."
            ) from None

    @application.post(
        "/v1/sandboxes/{sandbox_id}/feedback",
        response_model=FeedbackReceipt,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["feedback"],
    )
    def submit_feedback(
        sandbox_id: UUID,
        request: FeedbackRequest,
        sandbox_token: Annotated[str, Header(alias="X-Sandbox-Token", min_length=1)],
    ) -> Feedback:
        require_ready_sandbox(sandbox_id, sandbox_token)
        valid_chunk_ids = {chunk.id for chunk in evaluation_service.chunks(sandbox_id)}
        if request.relevant_chunk_id not in valid_chunk_ids:
            raise ServiceError(
                422,
                "INVALID_RELEVANT_CHUNK",
                "The feedback passage does not belong to this sandbox.",
            )
        return feedback_governance.submit(sandbox_id, **request.model_dump())

    return application


app = create_app()
