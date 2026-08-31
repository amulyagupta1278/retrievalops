import json
import logging
import re
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from numpy.typing import NDArray
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from retrievalops.api import create_app
from retrievalops.config import Settings
from retrievalops.telemetry import Telemetry


class DeterministicEmbedder:
    model_name = "test-hash-embedder-v1"

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]:
        vectors = np.zeros((len(texts), 16), dtype=np.float32)
        for row, text in enumerate(texts):
            for term in text.casefold().split():
                vectors[row, hash(term) % 16] += 1
        return vectors


class ContentLeakingErrorEmbedder(DeterministicEmbedder):
    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]:
        raise RuntimeError("PRIVATE-DOCUMENT-SENTINEL")


class RecordingExporter(SpanExporter):
    endpoint: str | None = None
    exported = 0

    def __init__(self, *, endpoint: str) -> None:
        type(self).endpoint = endpoint

    def export(self, spans) -> SpanExportResult:  # type: ignore[no-untyped-def]
        type(self).exported += len(spans)
        return SpanExportResult.SUCCESS


def _app(tmp_path: Path):  # type: ignore[no-untyped-def]
    return create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        )
    )


def test_request_events_are_correlated_structured_and_privacy_safe(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    sentinel = "never-record-this-document-or-query"
    app = _app(tmp_path)

    with (
        caplog.at_level(logging.INFO, logger="retrievalops.telemetry"),
        TestClient(app) as client,
    ):
        response = client.get(
            "/healthz",
            headers={"X-Request-ID": "demo-request-42", "X-Private-Value": sentinel},
        )

    assert response.headers["X-Request-ID"] == "demo-request-42"
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Trace-ID"])
    event = json.loads(caplog.records[-1].message)
    assert event == {
        "duration_ms": event["duration_ms"],
        "event": "http_request_completed",
        "method": "GET",
        "request_id": "demo-request-42",
        "route": "/healthz",
        "status_class": "2xx",
        "trace_id": response.headers["X-Trace-ID"],
    }
    assert sentinel not in caplog.text


def test_metrics_use_route_templates_and_bounded_labels(tmp_path: Path) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        client.get("/healthz")
        response = client.get("/metrics")

    assert response.status_code == 200
    health_metric = (
        'retrievalops_http_requests_total{method="GET",route="/healthz",status_class="2xx"} 1.0'
    )
    assert health_metric in response.text
    assert "request_id=" not in response.text
    assert "sandbox_id=" not in response.text
    assert "trace_id=" not in response.text


def test_one_request_produces_an_exportable_trace(tmp_path: Path) -> None:
    app = _app(tmp_path)
    exporter = InMemorySpanExporter()
    app.state.telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))

    with TestClient(app) as client:
        response = client.get("/healthz")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "GET /healthz"
    assert f"{spans[0].context.trace_id:032x}" == response.headers["X-Trace-ID"]
    assert spans[0].attributes["http.route"] == "/healthz"


def test_unhandled_exception_details_are_not_recorded_in_telemetry(caplog) -> None:  # type: ignore[no-untyped-def]
    sentinel = "PRIVATE-UNHANDLED-EXCEPTION-SENTINEL"
    telemetry = Telemetry(service_name="test", service_version="1")
    exporter = InMemorySpanExporter()
    telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = FastAPI()
    telemetry.install(app)

    @app.get("/explode")
    def explode() -> None:
        raise RuntimeError(sentinel)

    with (
        caplog.at_level(logging.INFO, logger="retrievalops.telemetry"),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/explode")

    spans = exporter.get_finished_spans()
    assert response.status_code == 500
    assert len(spans) == 1
    assert spans[0].events == ()
    assert sentinel not in caplog.text


def test_configured_otlp_endpoint_exports_runtime_traces(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("retrievalops.telemetry.OTLPSpanExporter", RecordingExporter)
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
            otlp_traces_endpoint="http://collector:4318/v1/traces",
        )
    )

    with TestClient(app) as client:
        client.get("/healthz")
        assert app.state.telemetry.tracer_provider.force_flush()

    assert RecordingExporter.endpoint == "http://collector:4318/v1/traces"
    assert RecordingExporter.exported == 1


def test_ingestion_retrieval_and_fallback_signals_are_emitted(tmp_path: Path) -> None:
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
                    "guide.txt",
                    b"Canary releases automatically rollback.",
                    "text/plain",
                )
            },
        ).json()
        app.state.ingestion_worker.process_next()
        query = client.post(
            f"/v1/sandboxes/{uploaded['sandbox_id']}/query",
            headers={"X-Sandbox-Token": uploaded["sandbox_token"]},
            json={"query": "automatic rollback"},
        )
        metrics = client.get("/metrics").text

    assert query.status_code == 200
    assert 'retrievalops_ingestion_job_transitions_total{state="ready"} 1.0' in metrics
    assert (
        'retrievalops_retrieval_requests_total{outcome="success",policy="bootstrap-hybrid"} 1.0'
        in metrics
    )
    assert 'retrievalops_fallbacks_total{reason="bootstrap_policy"} 1.0' in metrics


def test_upload_trace_propagates_into_worker_without_leaking_failures(
    tmp_path: Path, caplog
) -> None:  # type: ignore[no-untyped-def]
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        ),
        embedder=ContentLeakingErrorEmbedder(),
    )

    with (
        caplog.at_level(logging.INFO, logger="retrievalops.telemetry"),
        TestClient(app) as client,
    ):
        response = client.post(
            "/v1/sandboxes",
            files={"file": ("private.txt", b"PRIVATE-DOCUMENT-SENTINEL", "text/plain")},
        )
        app.state.ingestion_worker.process_next()

    job_id = response.json()["ingestion_job_id"]
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "retrievalops.telemetry"
        and json.loads(record.message)["event"] == "ingestion_job_transitioned"
        and json.loads(record.message)["job_id"] == job_id
    ]
    assert [event["state"] for event in events] == [
        "queued",
        "validating",
        "extracting",
        "indexing",
        "failed",
    ]
    assert {event["trace_id"] for event in events} == {response.headers["X-Trace-ID"]}
    assert "PRIVATE-DOCUMENT-SENTINEL" not in caplog.text


def test_metric_labels_reject_unbounded_values() -> None:
    telemetry = Telemetry(service_name="test", service_version="1")

    with pytest.raises(ValueError, match="policy"):
        telemetry.record_retrieval("user-controlled-policy", "success")
    with pytest.raises(ValueError, match="reason"):
        telemetry.record_fallback("raw exception message")


def test_every_alert_has_a_runbook_and_supported_severity() -> None:
    rules_path = Path("deploy/observability/alerts.yml")
    rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))["groups"]

    assert rules
    for group in rules:
        for rule in group["rules"]:
            assert rule["labels"]["severity"] in {"page", "ticket"}
            assert rule["annotations"]["runbook_url"].startswith("https://")
            assert rule["for"]
