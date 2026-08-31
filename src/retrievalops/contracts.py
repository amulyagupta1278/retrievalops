from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SupportedMediaType = Literal["application/pdf", "text/plain", "text/markdown"]


class JobState(StrEnum):
    queued = "queued"
    validating = "validating"
    extracting = "extracting"
    indexing = "indexing"
    ready = "ready"
    failed = "failed"


_ALLOWED_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.queued: frozenset({JobState.validating, JobState.failed}),
    JobState.validating: frozenset({JobState.extracting, JobState.failed}),
    JobState.extracting: frozenset({JobState.indexing, JobState.failed}),
    JobState.indexing: frozenset({JobState.ready, JobState.failed}),
    JobState.ready: frozenset(),
    JobState.failed: frozenset(),
}


class Sandbox(BaseModel):
    id: UUID
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def expiry_follows_creation(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self


class Document(BaseModel):
    id: UUID
    sandbox_id: UUID
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    media_type: SupportedMediaType
    size_bytes: Annotated[int, Field(gt=0, le=10 * 1024 * 1024)]
    sha256: Sha256


class Chunk(BaseModel):
    id: Annotated[str, Field(min_length=1)]
    document_id: UUID
    ordinal: Annotated[int, Field(ge=0)]
    text: Annotated[str, Field(min_length=1)]
    token_count: Annotated[int, Field(gt=0, le=512)]
    sha256: Sha256

    @model_validator(mode="after")
    def text_is_not_blank(self) -> Self:
        if not self.text.strip():
            raise ValueError("chunk text must not be blank")
        return self


class IngestionJob(BaseModel):
    id: UUID
    sandbox_id: UUID
    state: JobState
    error_code: str | None = None

    def transition_to(self, next_state: JobState) -> Self:
        if next_state not in _ALLOWED_JOB_TRANSITIONS[self.state]:
            raise ValueError(f"illegal job transition: {self.state} -> {next_state}")
        return self.model_copy(update={"state": next_state})


class CreateSandboxResponse(BaseModel):
    sandbox_id: UUID
    sandbox_token: str
    ingestion_job_id: UUID
    status: Literal["queued"] = "queued"
    expires_at: datetime


class Judgment(BaseModel):
    id: UUID
    sandbox_id: UUID
    query: Annotated[str, Field(min_length=1, max_length=1_000)]
    relevant_chunk_id: Annotated[str, Field(min_length=1)]
    relevance: Annotated[int, Field(ge=0, le=3)]
    reviewed: bool

    @model_validator(mode="after")
    def chunk_belongs_to_sandbox(self) -> Self:
        if not self.relevant_chunk_id.startswith(f"{self.sandbox_id}:"):
            raise ValueError("relevant chunk must belong to the judgment sandbox")
        return self


class RetrievalTrace(BaseModel):
    id: UUID
    sandbox_id: UUID
    query_hash: Sha256
    policy_name: Annotated[str, Field(min_length=1)]
    policy_version: Annotated[str, Field(min_length=1)]
    latency_ms: Annotated[float, Field(ge=0)]
    result_chunk_ids: Annotated[list[str], Field(min_length=1)]


class PolicyMetadata(BaseModel):
    name: Annotated[str, Field(min_length=1)]
    version: Annotated[str, Field(min_length=1)]
    corpus_hash: Sha256
    config_hash: Sha256
    index_hashes: Annotated[dict[str, Sha256], Field(min_length=1)]
    commit_sha: Annotated[str, Field(min_length=1)]


class EvaluationSuggestion(BaseModel):
    id: UUID
    query: Annotated[str, Field(min_length=1, max_length=1_000)]
    relevant_chunk_id: Annotated[str, Field(min_length=1)]
    passage: Annotated[str, Field(min_length=1)]


class CandidateMetrics(BaseModel):
    recall_at_10: Annotated[float, Field(ge=0, le=1)]
    ndcg_at_10: Annotated[float, Field(ge=0, le=1)]
    mrr_at_10: Annotated[float, Field(ge=0, le=1)]
    p50_latency_ms: Annotated[float, Field(ge=0)]
    p95_latency_ms: Annotated[float, Field(ge=0)]
    index_time_ms: Annotated[float, Field(ge=0)]
    estimated_cost_usd_per_1k_queries: Annotated[float, Field(ge=0)]


class CandidateScorecard(BaseModel):
    policy: Literal["bm25", "dense", "hybrid"]
    metrics: CandidateMetrics
    passed: bool
    rejection_reasons: list[str]


class PolicyDecision(BaseModel):
    active_policy: Literal["bootstrap-hybrid", "bm25", "dense", "hybrid"]
    policy_version: Annotated[str, Field(min_length=1)]
    evidence_hash: Sha256
    corpus_hash: Sha256
    configuration_hash: Sha256
    index_hashes: Annotated[dict[str, Sha256], Field(min_length=1)]
    scorecards: Annotated[list[CandidateScorecard], Field(min_length=3, max_length=3)]
    compiler_reason: Annotated[str, Field(min_length=1)]
