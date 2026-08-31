from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from retrievalops.contracts import (
    Chunk,
    Document,
    IngestionJob,
    JobState,
    Judgment,
    PolicyMetadata,
    RetrievalTrace,
    Sandbox,
)


def test_sandbox_requires_future_expiry() -> None:
    now = datetime(2026, 8, 31, tzinfo=UTC)

    with pytest.raises(ValidationError):
        Sandbox(id=uuid4(), created_at=now, expires_at=now)


def test_document_rejects_unsupported_media_type() -> None:
    with pytest.raises(ValidationError):
        Document(
            id=uuid4(),
            sandbox_id=uuid4(),
            filename="payload.exe",
            media_type="application/octet-stream",
            size_bytes=10,
            sha256="0" * 64,
        )


def test_chunk_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        Chunk(
            id="chunk-1",
            document_id=uuid4(),
            ordinal=0,
            text="   ",
            token_count=0,
            sha256="0" * 64,
        )


def test_job_state_rejects_illegal_transition() -> None:
    job = IngestionJob(id=uuid4(), sandbox_id=uuid4(), state=JobState.queued)

    with pytest.raises(ValueError, match="queued -> ready"):
        job.transition_to(JobState.ready)


def test_cross_sandbox_judgment_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Judgment(
            id=uuid4(),
            sandbox_id=uuid4(),
            query="What is RetrievalOps?",
            relevant_chunk_id="different-sandbox:chunk-1",
            relevance=1,
            reviewed=True,
        )


def test_trace_requires_ranked_results() -> None:
    with pytest.raises(ValidationError):
        RetrievalTrace(
            id=uuid4(),
            sandbox_id=uuid4(),
            query_hash="0" * 64,
            policy_name="bootstrap-hybrid",
            policy_version="1",
            latency_ms=1.0,
            result_chunk_ids=[],
        )


def test_policy_metadata_requires_complete_lineage() -> None:
    with pytest.raises(ValidationError):
        PolicyMetadata(
            name="bootstrap-hybrid",
            version="1",
            corpus_hash="0" * 64,
            config_hash="1" * 64,
            index_hashes={},
            commit_sha="development",
        )
