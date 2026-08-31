import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from retrievalops.contracts import (
    Feedback,
    FeedbackApproval,
    Judgment,
    PolicyDecision,
    RetrainingRun,
    RetrainingStatus,
)
from retrievalops.evaluation import EvaluationService
from retrievalops.metadata import MetadataStore
from retrievalops.storage import ArtifactStore
from retrievalops.telemetry import Telemetry

_TERM = re.compile(r"[A-Za-z0-9]+")
_DISTRIBUTION_BUCKETS = 32


class CandidateRegistrar(Protocol):
    def __call__(self, sandbox_id: UUID, decision: PolicyDecision) -> None: ...


class FeedbackGovernance:
    def __init__(self, metadata: MetadataStore) -> None:
        self._metadata = metadata
        self._retraining_handler: Callable[[UUID], RetrainingRun] | None = None

    def bind_retraining(self, handler: Callable[[UUID], RetrainingRun]) -> None:
        if self._retraining_handler is not None:
            raise RuntimeError("retraining handler is already configured")
        self._retraining_handler = handler

    def submit(
        self,
        sandbox_id: UUID,
        *,
        query: str,
        relevant_chunk_id: str,
        relevance: int,
    ) -> Feedback:
        feedback = Feedback(
            id=uuid4(),
            sandbox_id=sandbox_id,
            query=query,
            relevant_chunk_id=relevant_chunk_id,
            relevance=relevance,
            submitted_at=datetime.now(UTC),
        )
        self._metadata.create_feedback(feedback)
        return feedback

    def approve(
        self,
        sandbox_id: str | UUID,
        feedback_ids: list[str | UUID],
        *,
        approved_by: str,
        reason: str,
    ) -> FeedbackApproval:
        identifier = UUID(str(sandbox_id))
        ids = [UUID(str(value)) for value in feedback_ids]
        available = {item.id: item for item in self._metadata.feedback(identifier)}
        if len(set(ids)) != len(ids) or any(value not in available for value in ids):
            raise ValueError("feedback approval contains duplicate or unknown records")
        evidence = [
            {
                "id": str(value),
                "query_hash": hashlib.sha256(available[value].query.encode()).hexdigest(),
                "relevant_chunk_id": available[value].relevant_chunk_id,
                "relevance": available[value].relevance,
            }
            for value in sorted(ids)
        ]
        evidence_hash = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        approval = FeedbackApproval(
            audit_id=uuid4(),
            sandbox_id=identifier,
            approved=len(set(ids)),
            evidence_hash=evidence_hash,
            approved_by=approved_by,
            reason=reason,
            approved_at=datetime.now(UTC),
        )
        self._metadata.approve_feedback(approval, ids)
        if self._retraining_handler is not None:
            self._retraining_handler(identifier)
        return approval


class RetrainingWorkflow:
    def __init__(
        self,
        metadata: MetadataStore,
        artifacts: ArtifactStore,
        evaluation: EvaluationService,
        telemetry: Telemetry,
        *,
        minimum_approved_feedback: int,
        query_drift_threshold: float,
        registrar: CandidateRegistrar | None = None,
    ) -> None:
        self._metadata = metadata
        self._artifacts = artifacts
        self._evaluation = evaluation
        self._telemetry = telemetry
        self._minimum = minimum_approved_feedback
        self._threshold = query_drift_threshold
        self._registrar = registrar

    def run(self, sandbox_id: str | UUID) -> RetrainingRun:
        identifier = UUID(str(sandbox_id))
        approved = [
            item
            for item in self._metadata.feedback(identifier, approved_only=True)
            if item.relevance > 0
        ]
        if len(approved) < self._minimum:
            self._telemetry.record_drift("stable")
            return RetrainingRun(
                sandbox_id=identifier,
                status=RetrainingStatus.not_triggered,
                approved_evidence_count=len(approved),
            )

        baseline = self._metadata.judgments(identifier, reviewed_only=True)
        reasons = self._drift_reasons(identifier, baseline, approved)
        if not reasons:
            self._telemetry.record_drift("stable")
            return RetrainingRun(
                sandbox_id=identifier,
                status=RetrainingStatus.not_triggered,
                approved_evidence_count=len(approved),
            )

        self._telemetry.record_drift("detected")
        key = _idempotency_key(identifier, approved, reasons)
        existing = self._metadata.retraining_run(key)
        if existing is not None:
            return existing
        run = RetrainingRun(
            retraining_id=uuid4(),
            sandbox_id=identifier,
            idempotency_key=key,
            status=RetrainingStatus.running,
            approved_evidence_count=len(approved),
            drift_reasons=reasons,
        )
        if not self._metadata.create_retraining_run(run):
            concurrent = self._metadata.retraining_run(key)
            if concurrent is None:
                raise RuntimeError("idempotent retraining insert lost without an existing run")
            return concurrent

        self._telemetry.record_drift("workflow_started")
        judgments = baseline + [
            Judgment(
                id=uuid5(identifier, f"approved-feedback:{item.id}"),
                sandbox_id=identifier,
                query=item.query,
                relevant_chunk_id=item.relevant_chunk_id,
                relevance=item.relevance,
                reviewed=True,
            )
            for item in approved
        ]
        try:
            decision = self._evaluation.evaluate_candidate(identifier, judgments)
            if self._registrar is not None:
                self._registrar(identifier, decision)
            self._artifacts.write_json(
                identifier,
                "candidate_policy.json",
                decision.model_dump(mode="json"),
            )
        except Exception:
            self._telemetry.record_drift("workflow_failed")
            return self._metadata.finish_retraining_run(
                key,
                status=RetrainingStatus.failed,
                error_code="RETRAINING_FAILED",
            )
        return self._metadata.finish_retraining_run(
            key,
            status=RetrainingStatus.candidate_ready,
            policy_version=decision.policy_version,
        )

    def _drift_reasons(
        self,
        sandbox_id: UUID,
        baseline: list[Judgment],
        approved: list[Feedback],
    ) -> list[str]:
        active = json.loads(self._artifacts.read(f"{sandbox_id}/active_policy.json"))
        manifest = json.loads(self._artifacts.read(f"{sandbox_id}/index_manifest.json"))
        reasons: list[str] = []
        if active["corpus_hash"] != manifest["document_sha256"]:
            reasons.append("corpus_hash_changed")
        baseline_queries = [item.query for item in baseline]
        approved_queries = [item.query for item in approved]
        if _jensen_shannon(baseline_queries, approved_queries) >= self._threshold:
            reasons.append("query_distribution_shift")
        return reasons


def _idempotency_key(sandbox_id: UUID, approved: list[Feedback], reasons: list[str]) -> str:
    material = {
        "sandbox_id": str(sandbox_id),
        "feedback": [
            {
                "id": str(item.id),
                "query_hash": hashlib.sha256(item.query.encode()).hexdigest(),
                "relevant_chunk_id": item.relevant_chunk_id,
                "relevance": item.relevance,
            }
            for item in approved
        ],
        "reasons": reasons,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _jensen_shannon(baseline: list[str], current: list[str]) -> float:
    left = _distribution(baseline)
    right = _distribution(current)
    midpoint = [(first + second) / 2 for first, second in zip(left, right, strict=True)]
    return (_kl_divergence(left, midpoint) + _kl_divergence(right, midpoint)) / 2


def _distribution(queries: list[str]) -> list[float]:
    counts: Counter[int] = Counter()
    for query in queries:
        for term in _TERM.findall(query.casefold()):
            bucket = hashlib.sha256(term.encode()).digest()[0] % _DISTRIBUTION_BUCKETS
            counts[bucket] += 1
    total = sum(counts.values())
    if total == 0:
        return [1 / _DISTRIBUTION_BUCKETS] * _DISTRIBUTION_BUCKETS
    return [counts[index] / total for index in range(_DISTRIBUTION_BUCKETS)]


def _kl_divergence(values: list[float], reference: list[float]) -> float:
    return sum(
        value * math.log2(value / expected)
        for value, expected in zip(values, reference, strict=True)
        if value > 0 and expected > 0
    )
