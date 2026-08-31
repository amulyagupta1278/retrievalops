import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import numpy as np
from fastapi.testclient import TestClient
from numpy.typing import NDArray

from retrievalops.api import create_app
from retrievalops.config import Settings
from retrievalops.contracts import Chunk, JobState
from retrievalops.retrieval import (
    BM25Index,
    DenseIndex,
    SentenceTransformerEmbedder,
    reciprocal_rank_fusion,
)


class DeterministicEmbedder:
    model_name = "test-hash-embedder-v1"

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]:
        vectors = np.zeros((len(texts), 32), dtype=np.float32)
        for row, text in enumerate(texts):
            for term in text.casefold().split():
                vectors[row, hashlib.sha256(term.encode()).digest()[0] % 32] += 1
        return vectors


class FailingEmbedder(DeterministicEmbedder):
    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]:
        raise RuntimeError("simulated model failure")


def _chunk(ordinal: int, text: str) -> Chunk:
    sandbox_id = uuid4()
    return Chunk(
        id=f"{sandbox_id}:fixture:{ordinal:06d}",
        document_id=uuid4(),
        ordinal=ordinal,
        text=text,
        token_count=len(text.split()),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def test_bm25_dense_and_hybrid_rank_relevant_chunks() -> None:
    chunks = [
        _chunk(0, "deployment rollback canary"),
        _chunk(1, "pasta recipe tomato"),
        _chunk(2, "zero downtime canary deployment"),
    ]
    embedder = DeterministicEmbedder()

    lexical = BM25Index.build(chunks).search("canary deployment", 3)
    dense_index = DenseIndex.build(chunks, embedder)
    dense = dense_index.search("canary deployment", 3, embedder)
    hybrid = reciprocal_rank_fusion([lexical, dense], 3)

    assert lexical[0].chunk_id in {chunks[0].id, chunks[2].id}
    assert dense[0].chunk_id in {chunks[0].id, chunks[2].id}
    assert hybrid[0].chunk_id in {chunks[0].id, chunks[2].id}


def test_production_embedder_pins_model_revision() -> None:
    embedder = SentenceTransformerEmbedder()

    assert embedder.model_revision == "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


def test_upload_process_status_and_protected_hybrid_query(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/sandboxes",
            files={
                "file": (
                    "operations.txt",
                    b"Canary deployment protects users with automatic rollback.",
                    "text/plain",
                )
            },
        ).json()
        headers = {"X-Sandbox-Token": uploaded["sandbox_token"]}

        before = client.post(
            f"/v1/sandboxes/{uploaded['sandbox_id']}/query",
            headers=headers,
            json={"query": "How does rollback protect users?"},
        )
        assert before.status_code == 409
        assert app.state.ingestion_worker.process_next().state == JobState.ready

        manifest = json.loads(
            (tmp_path / "artifacts" / uploaded["sandbox_id"] / "index_manifest.json").read_text()
        )

        status_response = client.get(f"/v1/jobs/{uploaded['ingestion_job_id']}", headers=headers)
        response = client.post(
            f"/v1/sandboxes/{uploaded['sandbox_id']}/query",
            headers=headers,
            json={"query": "How does rollback protect users?"},
        )

        assert status_response.json()["state"] == "ready"
        assert len(manifest["configuration_sha256"]) == 64
        assert len(manifest["embedder_identity_sha256"]) == 64
        assert manifest["embedder_revision"] == "unversioned-test-double"
        assert response.status_code == 200
        assert response.json()["policy"] == "bootstrap-hybrid"
        assert response.json()["results"][0]["text"].startswith("Canary deployment")

        other = client.post(
            "/v1/sandboxes",
            files={"file": ("other.txt", b"Another isolated corpus", "text/plain")},
        ).json()
        denied = client.post(
            f"/v1/sandboxes/{uploaded['sandbox_id']}/query",
            headers={"X-Sandbox-Token": other["sandbox_token"]},
            json={"query": "rollback"},
        )
        assert denied.status_code == 404


def test_index_failure_never_marks_sandbox_ready(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        ),
        embedder=FailingEmbedder(),
    )
    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/sandboxes",
            files={"file": ("failure.txt", b"Valid source text", "text/plain")},
        ).json()
        result = app.state.ingestion_worker.process_next()
        response = client.get(
            f"/v1/jobs/{uploaded['ingestion_job_id']}",
            headers={"X-Sandbox-Token": uploaded["sandbox_token"]},
        )

    assert result.state == JobState.failed
    assert response.json()["state"] == "failed"
    assert response.json()["error_code"] == "INGESTION_FAILED"
    assert not list((tmp_path / "artifacts").rglob("index_manifest.json"))
