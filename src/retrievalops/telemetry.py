import json
import logging
import re
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Final
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from starlette.routing import Match

_LOGGER = logging.getLogger("retrievalops.telemetry")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_HTTP_BUCKETS: Final = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
_POLICIES: Final = frozenset({"bootstrap-hybrid", "bm25", "dense", "hybrid"})
_RETRIEVAL_OUTCOMES: Final = frozenset({"success", "unavailable"})
_JOB_STATES: Final = frozenset(
    {"queued", "validating", "extracting", "indexing", "ready", "failed"}
)
_FALLBACK_REASONS: Final = frozenset({"bootstrap_policy", "candidate_load_failed"})
_DRIFT_OUTCOMES: Final = frozenset({"stable", "detected", "workflow_started", "workflow_failed"})
_RELEASE_KINDS: Final = frozenset({"policy", "application"})
_RELEASE_OUTCOMES: Final = frozenset({"started", "promoted", "aborted", "rolled_back"})


class Telemetry:
    """One application's privacy-safe logs, metrics, and trace provider."""

    def __init__(
        self,
        *,
        service_name: str,
        service_version: str,
        otlp_traces_endpoint: str | None = None,
    ) -> None:
        self.registry = CollectorRegistry()
        self.tracer_provider = TracerProvider(
            resource=Resource.create(
                {"service.name": service_name, "service.version": service_version}
            )
        )
        if otlp_traces_endpoint is not None:
            self.tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_traces_endpoint))
            )
        self.tracer = self.tracer_provider.get_tracer("retrievalops")
        self.http_requests = Counter(
            "retrievalops_http_requests",
            "HTTP requests by stable route and status class.",
            ("method", "route", "status_class"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "retrievalops_http_request_duration_seconds",
            "HTTP request latency by stable route and status class.",
            ("method", "route", "status_class"),
            buckets=_HTTP_BUCKETS,
            registry=self.registry,
        )
        self.retrieval_requests = Counter(
            "retrievalops_retrieval_requests",
            "Retrieval requests by policy and outcome.",
            ("policy", "outcome"),
            registry=self.registry,
        )
        self.job_transitions = Counter(
            "retrievalops_ingestion_job_transitions",
            "Ingestion job transitions by destination state.",
            ("state",),
            registry=self.registry,
        )
        self.fallbacks = Counter(
            "retrievalops_fallbacks",
            "Safe retrieval fallbacks by bounded reason.",
            ("reason",),
            registry=self.registry,
        )
        self.drift_events = Counter(
            "retrievalops_drift_events",
            "Drift evaluations by bounded outcome.",
            ("outcome",),
            registry=self.registry,
        )
        self.release_events = Counter(
            "retrievalops_release_events",
            "Policy and application release outcomes.",
            ("kind", "outcome"),
            registry=self.registry,
        )
        for outcome in _DRIFT_OUTCOMES:
            self.drift_events.labels(outcome)
        for kind in _RELEASE_KINDS:
            for outcome in _RELEASE_OUTCOMES:
                self.release_events.labels(kind, outcome)

    def record_retrieval(self, policy: str, outcome: str) -> None:
        _require_bounded("policy", policy, _POLICIES)
        _require_bounded("outcome", outcome, _RETRIEVAL_OUTCOMES)
        self.retrieval_requests.labels(policy, outcome).inc()

    def record_job_transition(self, job_id: str, state: str, trace_id: str) -> None:
        _require_bounded("state", state, _JOB_STATES)
        self.job_transitions.labels(state).inc()
        _log_event("ingestion_job_transitioned", job_id=job_id, state=state, trace_id=trace_id)

    def record_fallback(self, reason: str) -> None:
        _require_bounded("reason", reason, _FALLBACK_REASONS)
        self.fallbacks.labels(reason).inc()

    def record_drift(self, outcome: str) -> None:
        _require_bounded("outcome", outcome, _DRIFT_OUTCOMES)
        self.drift_events.labels(outcome).inc()

    def record_release(self, kind: str, outcome: str) -> None:
        _require_bounded("kind", kind, _RELEASE_KINDS)
        _require_bounded("outcome", outcome, _RELEASE_OUTCOMES)
        self.release_events.labels(kind, outcome).inc()

    def install(self, application: FastAPI) -> None:
        application.state.telemetry = self

        @application.middleware("http")
        async def observe_http(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            supplied_id = request.headers.get("X-Request-ID", "")
            request_id = supplied_id if _SAFE_REQUEST_ID.fullmatch(supplied_id) else uuid4().hex
            request.state.request_id = request_id
            started = perf_counter()
            response: Response | None = None
            with self.tracer.start_as_current_span(
                "HTTP request",
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                trace_id = f"{span.get_span_context().trace_id:032x}"
                request.state.trace_id = trace_id
                try:
                    response = await call_next(request)
                    status_code = response.status_code
                except Exception:
                    status_code = 500
                    span.set_status(Status(StatusCode.ERROR, "INTERNAL_ERROR"))
                    raise
                finally:
                    duration = perf_counter() - started
                    route = _route_template(request)
                    status_class = f"{status_code // 100}xx"
                    span.update_name(f"{request.method} {route}")
                    span.set_attribute("http.request.method", request.method)
                    span.set_attribute("http.route", route)
                    span.set_attribute("http.response.status_code", status_code)
                    span.set_attribute("retrievalops.request_id", request_id)
                    self.http_requests.labels(request.method, route, status_class).inc()
                    self.http_duration.labels(request.method, route, status_class).observe(duration)
                    _log_event(
                        "http_request_completed",
                        duration_ms=round(duration * 1_000, 3),
                        method=request.method,
                        request_id=request_id,
                        route=route,
                        status_class=status_class,
                        trace_id=trace_id,
                    )
            assert response is not None
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Trace-ID"] = trace_id
            return response

        @application.get("/metrics", include_in_schema=False)
        def metrics() -> Response:
            return Response(generate_latest(self.registry), media_type=CONTENT_TYPE_LATEST)


def _route_template(request: Request) -> str:
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match is Match.FULL:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                return path
    return "unmatched"


def _require_bounded(name: str, value: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"unsupported {name} metric label")


def _log_event(event: str, **fields: str | float) -> None:
    _LOGGER.info(json.dumps({"event": event, **fields}, sort_keys=True))
