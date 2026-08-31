import hashlib
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from numpy.typing import NDArray

from retrievalops.api import create_app
from retrievalops.config import Settings


class DeterministicEmbedder:
    model_name = "test-hash-embedder-v1"

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]:
        vectors = np.zeros((len(texts), 32), dtype=np.float32)
        for row, text in enumerate(texts):
            for term in text.casefold().split():
                vectors[row, hashlib.sha256(term.encode()).digest()[0] % 32] += 1
        return vectors


def _ready_sandbox(client: TestClient, app: object) -> tuple[dict[str, str], dict[str, str]]:
    uploaded = client.post(
        "/v1/sandboxes",
        files={
            "file": (
                "release.md",
                b"# Safe releases\nCanary deployment uses health gates. "
                b"Automatic rollback protects users.",
                "text/markdown",
            )
        },
    ).json()
    app.state.ingestion_worker.process_next()  # type: ignore[attr-defined]
    return uploaded, {"X-Sandbox-Token": uploaded["sandbox_token"]}


def test_suggestions_are_five_deterministic_and_passage_linked(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded, headers = _ready_sandbox(client, app)
        url = f"/v1/sandboxes/{uploaded['sandbox_id']}/evaluation-suggestions"
        first = client.get(url, headers=headers)
        second = client.get(url, headers=headers)

    assert first.status_code == 200
    assert first.json() == second.json()
    assert len(first.json()) == 5
    assert len({item["query"] for item in first.json()}) == 5
    assert all(item["passage"] for item in first.json())


def test_optimization_requires_three_human_reviewed_judgments(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded, headers = _ready_sandbox(client, app)
        base = f"/v1/sandboxes/{uploaded['sandbox_id']}"
        suggestions = client.get(f"{base}/evaluation-suggestions", headers=headers).json()
        judgments = [
            {
                "query": item["query"],
                "relevant_chunk_id": item["relevant_chunk_id"],
                "relevance": 0 if index == 2 else 3,
                "reviewed": True,
            }
            for index, item in enumerate(suggestions[:3])
        ]
        stored = client.put(f"{base}/judgments", headers=headers, json={"judgments": judgments})
        blocked = client.post(f"{base}/optimize", headers=headers)

    assert stored.json()["optimization_unlocked"] is False
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "INSUFFICIENT_REVIEWED_JUDGMENTS"


def test_review_benchmark_compile_activate_and_query_champion(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        uploaded, headers = _ready_sandbox(client, app)
        base = f"/v1/sandboxes/{uploaded['sandbox_id']}"
        suggestions = client.get(f"{base}/evaluation-suggestions", headers=headers).json()
        judgments = [
            {
                "query": item["query"],
                "relevant_chunk_id": item["relevant_chunk_id"],
                "relevance": 3,
                "reviewed": True,
            }
            for item in suggestions[:3]
        ]
        stored = client.put(f"{base}/judgments", headers=headers, json={"judgments": judgments})
        optimized = client.post(f"{base}/optimize", headers=headers)
        repeated = client.post(f"{base}/optimize", headers=headers)
        policy = client.get(f"{base}/policy", headers=headers)
        query = client.post(
            f"{base}/query", headers=headers, json={"query": "How are users protected?"}
        )

    payload = optimized.json()
    assert stored.json()["optimization_unlocked"] is True
    assert optimized.status_code == 200
    assert repeated.json() == payload
    assert policy.json() == payload
    assert {card["policy"] for card in payload["scorecards"]} == {"bm25", "dense", "hybrid"}
    assert len(payload["corpus_hash"]) == 64
    assert len(payload["configuration_hash"]) == 64
    assert payload["index_hashes"]
    assert all(
        set(card["metrics"])
        == {
            "recall_at_10",
            "ndcg_at_10",
            "mrr_at_10",
            "p50_latency_ms",
            "p95_latency_ms",
            "index_time_ms",
            "estimated_cost_usd_per_1k_queries",
        }
        for card in payload["scorecards"]
    )
    assert all(card["metrics"]["recall_at_10"] == 1 for card in payload["scorecards"])
    assert all(card["metrics"]["ndcg_at_10"] == 1 for card in payload["scorecards"])
    assert all(card["metrics"]["mrr_at_10"] == 1 for card in payload["scorecards"])
    assert query.json()["policy"] == payload["active_policy"]
    assert query.json()["policy_version"] == payload["policy_version"]
    assert len(list((tmp_path / "artifacts").rglob("policy-*.json"))) == 1


def test_judgment_cannot_reference_another_sandbox(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        ),
        embedder=DeterministicEmbedder(),
    )
    with TestClient(app) as client:
        first, first_headers = _ready_sandbox(client, app)
        second, second_headers = _ready_sandbox(client, app)
        app.state.ingestion_worker.process_next()
        second_suggestion = client.get(
            f"/v1/sandboxes/{second['sandbox_id']}/evaluation-suggestions",
            headers=second_headers,
        ).json()[0]
        response = client.put(
            f"/v1/sandboxes/{first['sandbox_id']}/judgments",
            headers=first_headers,
            json={
                "judgments": [
                    {
                        "query": "Cross tenant query",
                        "relevant_chunk_id": second_suggestion["relevant_chunk_id"],
                        "relevance": 3,
                        "reviewed": True,
                    }
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RELEVANT_CHUNK"
