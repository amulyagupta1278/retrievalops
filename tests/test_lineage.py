import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from mlflow import MlflowClient
from numpy.typing import NDArray
from pydantic import ValidationError

from retrievalops.api import create_app
from retrievalops.config import Settings
from retrievalops.lineage import LineageRecord, LineageRegistry, controlled_lineage

ROOT = Path(__file__).parents[1]
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


def _controlled_record() -> LineageRecord:
    evidence = json.loads(
        (
            ROOT / "evidence" / "controlled-benchmarks" / "government-schemes-pilot-v2-seed42.json"
        ).read_text()
    )
    return controlled_lineage(evidence, commit_sha=COMMIT_SHA, dependency_lock_hash=LOCK_HASH)


def test_missing_required_lineage_is_rejected() -> None:
    payload = _controlled_record().model_dump()
    del payload["dependency_lock_hash"]

    with pytest.raises(ValidationError):
        LineageRecord.model_validate(payload)


def test_mlflow_configuration_requires_reconstructable_build_hashes(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="build_sha"):
        Settings(
            mlflow_tracking_uri=f"sqlite:///{tmp_path / 'mlflow.db'}",
            build_sha="development",
            dependency_lock_hash=LOCK_HASH,
        )


def test_controlled_and_ephemeral_namespaces_register_and_reconstruct(tmp_path: Path) -> None:
    tracking_uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    registry = LineageRegistry(tracking_uri, tmp_path / "artifacts")
    controlled = _controlled_record()
    ephemeral = controlled.model_copy(
        update={"scope": "ephemeral", "subject_id": "00000000-0000-0000-0000-000000000001"}
    )

    controlled_registration = registry.register(controlled)
    repeated = registry.register(controlled)
    ephemeral_registration = registry.register(ephemeral)

    assert controlled_registration.experiment_name == "retrievalops-controlled"
    assert ephemeral_registration.experiment_name == "retrievalops-ephemeral"
    assert repeated.model_version == controlled_registration.model_version
    assert repeated.run_id == controlled_registration.run_id
    reconstructed = registry.reconstruct(controlled_registration.registered_model_name)
    assert reconstructed == controlled
    client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
    alias = client.get_model_version_by_alias(
        controlled_registration.registered_model_name, "champion"
    )
    assert str(alias.version) == controlled_registration.model_version


def test_candidate_alias_does_not_replace_champion(tmp_path: Path) -> None:
    tracking_uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    registry = LineageRegistry(tracking_uri, tmp_path / "artifacts")
    champion = _controlled_record().model_copy(
        update={"scope": "ephemeral", "subject_id": "candidate-isolation"}
    )
    candidate = champion.model_copy(update={"evidence_hash": "f" * 64})

    champion_registration = registry.register(champion, alias="champion")
    candidate_registration = registry.register(candidate, alias="candidate")
    client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
    champion_alias = client.get_model_version_by_alias(
        champion_registration.registered_model_name, "champion"
    )
    candidate_alias = client.get_model_version_by_alias(
        candidate_registration.registered_model_name, "candidate"
    )

    assert champion_registration.alias == "champion"
    assert candidate_registration.alias == "candidate"
    assert str(champion_alias.version) == champion_registration.model_version
    assert str(candidate_alias.version) == candidate_registration.model_version


def test_ephemeral_registration_does_not_persist_document_or_query_text(tmp_path: Path) -> None:
    document_sentinel = "PRIVATE-DOCUMENT-CONTENT-6d64c8"
    query_sentinel = "PRIVATE-QUERY-CONTENT-3b2671"
    tracking_uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    app = create_app(
        Settings(
            storage_root=tmp_path / "sandbox-artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
            mlflow_tracking_uri=tracking_uri,
            mlflow_artifact_root=tmp_path / "mlflow-artifacts",
            build_sha=COMMIT_SHA,
            dependency_lock_hash=LOCK_HASH,
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/sandboxes",
            files={
                "file": (
                    "private.txt",
                    f"{document_sentinel} Canary rollback protects users.".encode(),
                    "text/plain",
                )
            },
        ).json()
        app.state.ingestion_worker.process_next()
        headers = {"X-Sandbox-Token": uploaded["sandbox_token"]}
        base = f"/v1/sandboxes/{uploaded['sandbox_id']}"
        suggestion = client.get(f"{base}/evaluation-suggestions", headers=headers).json()[0]
        judgments = {
            "judgments": [
                {
                    "query": f"{query_sentinel}-{index}",
                    "relevant_chunk_id": suggestion["relevant_chunk_id"],
                    "relevance": 3,
                    "reviewed": True,
                }
                for index in range(3)
            ]
        }
        assert client.put(f"{base}/judgments", headers=headers, json=judgments).status_code == 200
        optimized = client.post(f"{base}/optimize", headers=headers)
        assert optimized.status_code == 200, optimized.text

    persisted = (tmp_path / "mlflow.db").read_bytes()
    for artifact in (tmp_path / "mlflow-artifacts").rglob("*"):
        if artifact.is_file():
            persisted += artifact.read_bytes()
    assert document_sentinel.encode() not in persisted
    assert query_sentinel.encode() not in persisted
