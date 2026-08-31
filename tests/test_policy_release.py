import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import numpy as np
from fastapi.testclient import TestClient
from numpy.typing import NDArray

from retrievalops.api import create_app
from retrievalops.config import Settings
from retrievalops.contracts import CanaryObservation


class DeterministicEmbedder:
    model_name = "test-hash-embedder-v1"

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]:
        vectors = np.zeros((len(texts), 32), dtype=np.float32)
        for row, text in enumerate(texts):
            for term in text.casefold().split():
                vectors[row, hashlib.sha256(term.encode()).digest()[0] % 32] += 1
        return vectors


def _candidate_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        ),
        embedder=DeterministicEmbedder(),
    )
    client = TestClient(app)
    client.__enter__()
    uploaded = client.post(
        "/v1/sandboxes",
        files={
            "file": (
                "release.txt",
                b"Canary health gates promote safely. Automatic rollback protects users.",
                "text/plain",
            )
        },
    ).json()
    headers = {"X-Sandbox-Token": uploaded["sandbox_token"]}
    app.state.ingestion_worker.process_next()
    base = f"/v1/sandboxes/{uploaded['sandbox_id']}"
    suggestions = client.get(f"{base}/evaluation-suggestions", headers=headers).json()
    client.put(
        f"{base}/judgments",
        headers=headers,
        json={
            "judgments": [
                {
                    "query": item["query"],
                    "relevant_chunk_id": item["relevant_chunk_id"],
                    "relevance": 3,
                    "reviewed": True,
                }
                for item in suggestions[:3]
            ]
        },
    )
    champion = client.post(f"{base}/optimize", headers=headers).json()
    candidate = dict(champion)
    candidate["policy_version"] = "candidate-v2"
    candidate["evidence_hash"] = "f" * 64
    app.state.artifact_store.write_json(
        UUID(uploaded["sandbox_id"]), "candidate_policy.json", candidate
    )
    return app, client, uploaded, champion, candidate


def _healthy() -> CanaryObservation:
    return CanaryObservation(
        request_count=100,
        availability=1,
        error_rate=0,
        p95_latency_ms=20,
        fallback_rate=0,
        load_failures=0,
    )


def test_good_candidate_advances_10_50_100_and_promotes(tmp_path: Path) -> None:
    app, client, uploaded, champion, candidate = _candidate_app(tmp_path)
    try:
        sandbox_id = uploaded["sandbox_id"]
        started = app.state.policy_release_controller.start(sandbox_id)
        at_fifty = app.state.policy_release_controller.observe(sandbox_id, _healthy())
        at_hundred = app.state.policy_release_controller.observe(sandbox_id, _healthy())
        promoted = app.state.policy_release_controller.observe(sandbox_id, _healthy())
        active = json.loads(
            (tmp_path / "artifacts" / sandbox_id / "active_policy.json").read_text()
        )
    finally:
        client.__exit__(None, None, None)

    assert started.allocation_percent == 10
    assert at_fifty.allocation_percent == 50
    assert at_hundred.allocation_percent == 100
    assert promoted.status == "promoted"
    assert promoted.allocation_percent == 0
    assert active["policy_version"] == candidate["policy_version"]
    assert active["policy_version"] != champion["policy_version"]


def test_bad_operational_canary_resets_candidate_to_zero(tmp_path: Path) -> None:
    app, client, uploaded, champion, _ = _candidate_app(tmp_path)
    try:
        sandbox_id = uploaded["sandbox_id"]
        app.state.policy_release_controller.start(sandbox_id)
        aborted = app.state.policy_release_controller.observe(
            sandbox_id,
            CanaryObservation(
                request_count=100,
                availability=1,
                error_rate=0.02,
                p95_latency_ms=20,
                fallback_rate=0,
                load_failures=0,
            ),
        )
        active = json.loads(
            (tmp_path / "artifacts" / sandbox_id / "active_policy.json").read_text()
        )
    finally:
        client.__exit__(None, None, None)

    assert aborted.status == "aborted"
    assert aborted.allocation_percent == 0
    assert aborted.abort_reason == "ERROR_RATE_GATE_FAILED"
    assert active["policy_version"] == champion["policy_version"]


def test_trace_allocation_is_deterministic_and_matches_percentage(tmp_path: Path) -> None:
    app, client, uploaded, _, _ = _candidate_app(tmp_path)
    try:
        sandbox_id = uploaded["sandbox_id"]
        controller = app.state.policy_release_controller
        controller.start(sandbox_id)
        decisions = [controller.route(sandbox_id, UUID(int=value)) for value in range(1000)]
        repeated = [controller.route(sandbox_id, UUID(int=value)) for value in range(1000)]
    finally:
        client.__exit__(None, None, None)

    assert decisions == repeated
    candidate_fraction = sum(decision == "candidate" for decision in decisions) / len(decisions)
    assert 0.07 <= candidate_fraction <= 0.13


def test_offline_gate_rejects_slow_candidate_before_traffic(tmp_path: Path) -> None:
    app, client, uploaded, champion, candidate = _candidate_app(tmp_path)
    try:
        sandbox_id = uploaded["sandbox_id"]
        chosen = candidate["active_policy"]
        for scorecard in candidate["scorecards"]:
            if scorecard["policy"] == chosen:
                scorecard["metrics"]["p95_latency_ms"] = 501
        app.state.artifact_store.write_json(UUID(sandbox_id), "candidate_policy.json", candidate)
        rejected = app.state.policy_release_controller.start(sandbox_id)
        active = json.loads(
            (tmp_path / "artifacts" / sandbox_id / "active_policy.json").read_text()
        )
    finally:
        client.__exit__(None, None, None)

    assert rejected.status == "rejected"
    assert rejected.allocation_percent == 0
    assert rejected.abort_reason == "P95_LATENCY_GATE_FAILED"
    assert active["policy_version"] == champion["policy_version"]


def test_live_queries_route_between_loaded_champion_and_candidate(tmp_path: Path) -> None:
    app, client, uploaded, champion, candidate = _candidate_app(tmp_path)
    try:
        sandbox_id = uploaded["sandbox_id"]
        headers = {"X-Sandbox-Token": uploaded["sandbox_token"]}
        app.state.policy_release_controller.start(sandbox_id)
        observed: set[str] = set()
        for _ in range(100):
            response = client.post(
                f"/v1/sandboxes/{sandbox_id}/query",
                headers=headers,
                json={"query": "canary rollback"},
            )
            payload = response.json()
            target = app.state.policy_release_controller.route(
                sandbox_id, UUID(payload["trace_id"])
            )
            expected_version = (
                candidate["policy_version"] if target == "candidate" else champion["policy_version"]
            )
            assert payload["policy_version"] == expected_version
            observed.add(target)
    finally:
        client.__exit__(None, None, None)

    assert observed == {"candidate", "champion"}


def test_candidate_load_failure_falls_back_to_champion_without_restart(
    tmp_path: Path,
) -> None:
    app, client, uploaded, champion, _ = _candidate_app(tmp_path)
    try:
        sandbox_id = uploaded["sandbox_id"]
        headers = {"X-Sandbox-Token": uploaded["sandbox_token"]}
        app.state.policy_release_controller.start(sandbox_id)
        (tmp_path / "artifacts" / sandbox_id / "candidate_policy.json").write_text("invalid")
        candidate_allocations = 0
        for _ in range(100):
            response = client.post(
                f"/v1/sandboxes/{sandbox_id}/query",
                headers=headers,
                json={"query": "canary rollback"},
            )
            assert response.status_code == 200
            payload = response.json()
            if (
                app.state.policy_release_controller.route(sandbox_id, UUID(payload["trace_id"]))
                == "candidate"
            ):
                candidate_allocations += 1
                assert payload["policy_version"] == champion["policy_version"]
        metrics = client.get("/metrics").text
    finally:
        client.__exit__(None, None, None)

    assert candidate_allocations > 0
    assert 'retrievalops_fallbacks_total{reason="candidate_load_failed"}' in metrics


def test_candidate_version_change_mid_canary_aborts_before_promotion(tmp_path: Path) -> None:
    app, client, uploaded, champion, candidate = _candidate_app(tmp_path)
    try:
        sandbox_id = uploaded["sandbox_id"]
        controller = app.state.policy_release_controller
        controller.start(sandbox_id)
        candidate["policy_version"] = "unreviewed-replacement"
        app.state.artifact_store.write_json(UUID(sandbox_id), "candidate_policy.json", candidate)
        aborted = controller.observe(sandbox_id, _healthy())
        active = json.loads(
            (tmp_path / "artifacts" / sandbox_id / "active_policy.json").read_text()
        )
    finally:
        client.__exit__(None, None, None)

    assert aborted.status == "aborted"
    assert aborted.allocation_percent == 0
    assert aborted.abort_reason == "CANDIDATE_VERSION_CHANGED"
    assert active["policy_version"] == champion["policy_version"]
