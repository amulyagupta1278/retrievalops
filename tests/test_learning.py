import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import numpy as np
from fastapi.testclient import TestClient
from mlflow import MlflowClient
from numpy.typing import NDArray

from retrievalops.api import create_app
from retrievalops.config import Settings

COMMIT_SHA = "4a200cf1b198b7e3ff6f28d1d5f78f16ef2951c5"
LOCK_HASH = "097f287cd5707d3033c8f2beda0887b71a98b32be4744f45b774a013c1eadf81"


class DeterministicEmbedder:
    model_name = "test-hash-embedder-v1"

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]:
        vectors = np.zeros((len(texts), 32), dtype=np.float32)
        for row, text in enumerate(texts):
            for term in text.casefold().split():
                vectors[row, hashlib.sha256(term.encode()).digest()[0] % 32] += 1
        return vectors


def _optimized_sandbox(client: TestClient, app: object) -> tuple[dict[str, str], dict[str, str]]:
    uploaded = client.post(
        "/v1/sandboxes",
        files={
            "file": (
                "operations.txt",
                b"Canary deployment uses health gates. Automatic rollback protects users.",
                "text/plain",
            )
        },
    ).json()
    headers = {"X-Sandbox-Token": uploaded["sandbox_token"]}
    app.state.ingestion_worker.process_next()  # type: ignore[attr-defined]
    base = f"/v1/sandboxes/{uploaded['sandbox_id']}"
    suggestions = client.get(f"{base}/evaluation-suggestions", headers=headers).json()
    judgments = {
        "judgments": [
            {
                "query": item["query"],
                "relevant_chunk_id": item["relevant_chunk_id"],
                "relevance": 3,
                "reviewed": True,
            }
            for item in suggestions[:3]
        ]
    }
    assert client.put(f"{base}/judgments", headers=headers, json=judgments).status_code == 200
    assert client.post(f"{base}/optimize", headers=headers).status_code == 200
    return uploaded, headers


def test_unapproved_feedback_is_isolated_from_retraining(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded, headers = _optimized_sandbox(client, app)
        base = f"/v1/sandboxes/{uploaded['sandbox_id']}"
        suggestion = client.get(f"{base}/evaluation-suggestions", headers=headers).json()[0]
        feedback = client.post(
            f"{base}/feedback",
            headers=headers,
            json={
                "query": "quantum nebula unrelated workload",
                "relevant_chunk_id": suggestion["relevant_chunk_id"],
                "relevance": 3,
            },
        )
        active_before = (
            tmp_path / "artifacts" / uploaded["sandbox_id"] / "active_policy.json"
        ).read_bytes()
        result = app.state.retraining_workflow.run(uploaded["sandbox_id"])

    assert feedback.status_code == 202
    assert feedback.json()["status"] == "pending"
    assert set(feedback.json()) == {"id", "status", "submitted_at"}
    assert result.status == "not_triggered"
    assert result.approved_evidence_count == 0
    assert app.state.metadata_store.retraining_run_count(uploaded["sandbox_id"]) == 0
    assert (
        tmp_path / "artifacts" / uploaded["sandbox_id"] / "active_policy.json"
    ).read_bytes() == active_before


def test_approved_drift_starts_exactly_one_candidate_without_changing_champion(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
            drift_min_approved_feedback=3,
            query_drift_threshold=0.01,
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded, headers = _optimized_sandbox(client, app)
        base = f"/v1/sandboxes/{uploaded['sandbox_id']}"
        suggestion = client.get(f"{base}/evaluation-suggestions", headers=headers).json()[0]
        feedback_ids = []
        for ordinal in range(3):
            response = client.post(
                f"{base}/feedback",
                headers=headers,
                json={
                    "query": f"quantum nebula galaxy workload {ordinal}",
                    "relevant_chunk_id": suggestion["relevant_chunk_id"],
                    "relevance": 3,
                },
            )
            feedback_ids.append(response.json()["id"])

        active_path = tmp_path / "artifacts" / uploaded["sandbox_id"] / "active_policy.json"
        active_before = active_path.read_bytes()
        approval = app.state.feedback_governance.approve(
            uploaded["sandbox_id"],
            feedback_ids,
            approved_by="demo-reviewer",
            reason="Human review confirmed passage relevance.",
        )
        assert app.state.metadata_store.retraining_run_count(uploaded["sandbox_id"]) == 1
        first = app.state.retraining_workflow.run(uploaded["sandbox_id"])
        second = app.state.retraining_workflow.run(uploaded["sandbox_id"])
        metrics = client.get("/metrics").text

    assert approval.approved == 3
    assert approval.audit_id
    assert first.status == "candidate_ready"
    assert first.retraining_id == second.retraining_id
    assert first.idempotency_key == second.idempotency_key
    assert first.policy_version == second.policy_version
    assert app.state.metadata_store.retraining_run_count(uploaded["sandbox_id"]) == 1
    assert active_path.read_bytes() == active_before
    assert (active_path.parent / "candidate_policy.json").exists()
    assert 'retrievalops_drift_events_total{outcome="detected"} 3.0' in metrics
    assert 'retrievalops_drift_events_total{outcome="workflow_started"} 1.0' in metrics


def test_failed_retraining_preserves_champion(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
            drift_min_approved_feedback=3,
            query_drift_threshold=0.0,
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded, headers = _optimized_sandbox(client, app)
        base = f"/v1/sandboxes/{uploaded['sandbox_id']}"
        suggestion = client.get(f"{base}/evaluation-suggestions", headers=headers).json()[0]
        feedback_ids = [
            client.post(
                f"{base}/feedback",
                headers=headers,
                json={
                    "query": f"new workload {ordinal}",
                    "relevant_chunk_id": suggestion["relevant_chunk_id"],
                    "relevance": 3,
                },
            ).json()["id"]
            for ordinal in range(3)
        ]
        active_path = tmp_path / "artifacts" / uploaded["sandbox_id"] / "active_policy.json"
        active_before = active_path.read_bytes()
        (active_path.parent / "dense.faiss").write_bytes(b"corrupted")
        app.state.feedback_governance.approve(
            uploaded["sandbox_id"],
            feedback_ids,
            approved_by="demo-reviewer",
            reason="Reviewed before failure injection.",
        )
        result = app.state.retraining_workflow.run(uploaded["sandbox_id"])

    assert result.status == "failed"
    assert result.error_code == "RETRAINING_FAILED"
    assert active_path.read_bytes() == active_before


def test_feedback_is_sandbox_isolated_and_admin_operations_are_not_public(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        first, first_headers = _optimized_sandbox(client, app)
        second, second_headers = _optimized_sandbox(client, app)
        second_suggestion = client.get(
            f"/v1/sandboxes/{second['sandbox_id']}/evaluation-suggestions",
            headers=second_headers,
        ).json()[0]
        rejected = client.post(
            f"/v1/sandboxes/{first['sandbox_id']}/feedback",
            headers=first_headers,
            json={
                "query": "cross-sandbox evidence",
                "relevant_chunk_id": second_suggestion["relevant_chunk_id"],
                "relevance": 3,
            },
        )
        paths = client.get("/openapi.json").json()["paths"]

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "INVALID_RELEVANT_CHUNK"
    assert all("approve" not in path and "retrain" not in path for path in paths)


def test_deletion_removes_feedback_approvals_and_retraining_state(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
            drift_min_approved_feedback=3,
            query_drift_threshold=0.0,
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded, headers = _optimized_sandbox(client, app)
        sandbox_id = uploaded["sandbox_id"]
        base = f"/v1/sandboxes/{sandbox_id}"
        suggestion = client.get(f"{base}/evaluation-suggestions", headers=headers).json()[0]
        feedback_ids = [
            client.post(
                f"{base}/feedback",
                headers=headers,
                json={
                    "query": f"approved deletion evidence {ordinal}",
                    "relevant_chunk_id": suggestion["relevant_chunk_id"],
                    "relevance": 3,
                },
            ).json()["id"]
            for ordinal in range(3)
        ]
        app.state.feedback_governance.approve(
            sandbox_id,
            feedback_ids,
            approved_by="demo-reviewer",
            reason="Approved evidence must still expire.",
        )
        assert app.state.metadata_store.retraining_run_count(sandbox_id) == 1
        assert app.state.metadata_store.feedback_approval_count(sandbox_id) == 1
        deleted = client.delete(base, headers=headers)

    assert deleted.status_code == 204
    assert app.state.metadata_store.feedback(UUID(sandbox_id)) == []
    assert app.state.metadata_store.retraining_run_count(sandbox_id) == 0
    assert app.state.metadata_store.feedback_approval_count(sandbox_id) == 0
    assert not (tmp_path / "artifacts" / sandbox_id).exists()


def test_corpus_hash_drift_triggers_candidate_independently_of_query_drift(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
            drift_min_approved_feedback=3,
            query_drift_threshold=1.0,
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded, headers = _optimized_sandbox(client, app)
        sandbox_id = uploaded["sandbox_id"]
        base = f"/v1/sandboxes/{sandbox_id}"
        suggestions = client.get(f"{base}/evaluation-suggestions", headers=headers).json()
        feedback_ids = [
            client.post(
                f"{base}/feedback",
                headers=headers,
                json={
                    "query": item["query"],
                    "relevant_chunk_id": item["relevant_chunk_id"],
                    "relevance": 3,
                },
            ).json()["id"]
            for item in suggestions[:3]
        ]
        manifest_path = tmp_path / "artifacts" / sandbox_id / "index_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["document_sha256"] = "f" * 64
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        app.state.feedback_governance.approve(
            sandbox_id,
            feedback_ids,
            approved_by="demo-reviewer",
            reason="Controlled corpus drift simulation.",
        )
        run = app.state.retraining_workflow.run(sandbox_id)

    assert run.status == "candidate_ready"
    assert run.drift_reasons == ["corpus_hash_changed"]


def test_automatic_candidate_registration_keeps_mlflow_champion_alias(
    tmp_path: Path,
) -> None:
    feedback_sentinel = "PRIVATE-APPROVED-FEEDBACK-7d31"
    tracking_uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
            mlflow_tracking_uri=tracking_uri,
            mlflow_artifact_root=tmp_path / "mlflow-artifacts",
            build_sha=COMMIT_SHA,
            dependency_lock_hash=LOCK_HASH,
            drift_min_approved_feedback=3,
            query_drift_threshold=0.0,
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded, headers = _optimized_sandbox(client, app)
        sandbox_id = uploaded["sandbox_id"]
        base = f"/v1/sandboxes/{sandbox_id}"
        suggestion = client.get(f"{base}/evaluation-suggestions", headers=headers).json()[0]
        feedback_ids = [
            client.post(
                f"{base}/feedback",
                headers=headers,
                json={
                    "query": f"{feedback_sentinel} shifted workload {ordinal}",
                    "relevant_chunk_id": suggestion["relevant_chunk_id"],
                    "relevance": 3,
                },
            ).json()["id"]
            for ordinal in range(3)
        ]
        app.state.feedback_governance.approve(
            sandbox_id,
            feedback_ids,
            approved_by="demo-reviewer",
            reason="MLflow candidate isolation proof.",
        )

    client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
    model_name = f"retrievalops.ephemeral.{sandbox_id}"
    champion = client.get_model_version_by_alias(model_name, "champion")
    candidate = client.get_model_version_by_alias(model_name, "candidate")
    assert champion.version != candidate.version
    persisted = (tmp_path / "mlflow.db").read_bytes()
    for artifact in (tmp_path / "mlflow-artifacts").rglob("*"):
        if artifact.is_file():
            persisted += artifact.read_bytes()
    assert feedback_sentinel.encode() not in persisted
