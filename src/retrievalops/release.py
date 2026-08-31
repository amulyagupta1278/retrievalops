import hashlib
from typing import Protocol
from uuid import UUID

from retrievalops.contracts import (
    CanaryObservation,
    CandidateScorecard,
    PolicyDecision,
    PolicyReleaseState,
    PolicyReleaseStatus,
)
from retrievalops.querying import QueryHit, QueryService
from retrievalops.storage import ArtifactStore
from retrievalops.telemetry import Telemetry


class ChampionPromoter(Protocol):
    def __call__(self, sandbox_id: UUID) -> None: ...


class PolicyReleaseController:
    def __init__(
        self,
        artifacts: ArtifactStore,
        telemetry: Telemetry,
        promoter: ChampionPromoter | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._telemetry = telemetry
        self._promoter = promoter

    def start(self, sandbox_id: str | UUID) -> PolicyReleaseState:
        with self._telemetry.tracer.start_as_current_span("policy.release.start"):
            return self._start(sandbox_id)

    def _start(self, sandbox_id: str | UUID) -> PolicyReleaseState:
        identifier = UUID(str(sandbox_id))
        champion = self._policy(identifier, "active_policy.json")
        candidate = self._policy(identifier, "candidate_policy.json")
        rejection = self._load_rejection(identifier, candidate) or _offline_rejection(
            champion, candidate
        )
        state = PolicyReleaseState(
            sandbox_id=identifier,
            champion_version=champion.policy_version,
            candidate_version=candidate.policy_version,
            allocation_percent=0 if rejection else 10,
            status=PolicyReleaseStatus.rejected if rejection else PolicyReleaseStatus.canary,
            abort_reason=rejection,
        )
        self._write(state)
        self._telemetry.record_release("policy", "aborted" if rejection else "started")
        return state

    def observe(self, sandbox_id: str | UUID, observation: CanaryObservation) -> PolicyReleaseState:
        with self._telemetry.tracer.start_as_current_span("policy.release.observe"):
            return self._observe(sandbox_id, observation)

    def _observe(
        self, sandbox_id: str | UUID, observation: CanaryObservation
    ) -> PolicyReleaseState:
        identifier = UUID(str(sandbox_id))
        state = self.state(identifier)
        if state is None or state.status is not PolicyReleaseStatus.canary:
            raise ValueError("no active policy canary")
        failure = self._version_rejection(identifier, state) or _operational_rejection(observation)
        if failure is not None:
            aborted = state.model_copy(
                update={
                    "allocation_percent": 0,
                    "status": PolicyReleaseStatus.aborted,
                    "abort_reason": failure,
                }
            )
            self._write(aborted)
            self._telemetry.record_release("policy", "rolled_back")
            return aborted
        if state.allocation_percent == 10:
            advanced = state.model_copy(update={"allocation_percent": 50})
        elif state.allocation_percent == 50:
            advanced = state.model_copy(update={"allocation_percent": 100})
        else:
            candidate = self._policy(identifier, "candidate_policy.json")
            if self._promoter is not None:
                self._promoter(identifier)
            self._artifacts.write_json(
                identifier, "active_policy.json", candidate.model_dump(mode="json")
            )
            advanced = state.model_copy(
                update={
                    "champion_version": candidate.policy_version,
                    "allocation_percent": 0,
                    "status": PolicyReleaseStatus.promoted,
                }
            )
            self._telemetry.record_release("policy", "promoted")
        self._write(advanced)
        return advanced

    def route(self, sandbox_id: str | UUID, trace_id: UUID) -> str:
        identifier = UUID(str(sandbox_id))
        state = self.state(identifier)
        if (
            state is None
            or state.status is not PolicyReleaseStatus.canary
            or state.allocation_percent == 0
        ):
            return "champion"
        bucket = (
            int.from_bytes(hashlib.sha256(f"{identifier}:{trace_id}".encode()).digest()[:8], "big")
            % 100
        )
        return "candidate" if bucket < state.allocation_percent else "champion"

    def search(
        self,
        querying: QueryService,
        sandbox_id: UUID,
        trace_id: UUID,
        query: str,
        top_k: int,
    ) -> tuple[list[QueryHit], str, str]:
        if self.route(sandbox_id, trace_id) == "candidate":
            try:
                candidate = self._policy(sandbox_id, "candidate_policy.json")
                state = self.state(sandbox_id)
                if state is None or candidate.policy_version != state.candidate_version:
                    raise ValueError("candidate version changed during canary")
                hits = querying.search_policy(sandbox_id, query, top_k, candidate.active_policy)
                return hits, candidate.active_policy, candidate.policy_version
            except (OSError, ValueError, RuntimeError):
                self._telemetry.record_fallback("candidate_load_failed")
        hits, version = querying.search(sandbox_id, query, top_k)
        champion, _ = querying.active_policy(sandbox_id)
        return hits, champion, version

    def state(self, sandbox_id: str | UUID) -> PolicyReleaseState | None:
        identifier = UUID(str(sandbox_id))
        try:
            return PolicyReleaseState.model_validate_json(
                self._artifacts.read(f"{identifier}/policy_release.json")
            )
        except FileNotFoundError:
            return None

    def _policy(self, sandbox_id: UUID, name: str) -> PolicyDecision:
        return PolicyDecision.model_validate_json(self._artifacts.read(f"{sandbox_id}/{name}"))

    def _write(self, state: PolicyReleaseState) -> None:
        self._artifacts.write_json(
            state.sandbox_id, "policy_release.json", state.model_dump(mode="json")
        )

    def _version_rejection(self, sandbox_id: UUID, state: PolicyReleaseState) -> str | None:
        try:
            candidate = self._policy(sandbox_id, "candidate_policy.json")
        except (OSError, ValueError):
            return "CANDIDATE_LOAD_FAILED"
        if candidate.policy_version != state.candidate_version:
            return "CANDIDATE_VERSION_CHANGED"
        try:
            champion = self._policy(sandbox_id, "active_policy.json")
        except (OSError, ValueError):
            return "CHAMPION_LOAD_FAILED"
        if champion.policy_version != state.champion_version:
            return "CHAMPION_VERSION_CHANGED"
        return None

    def _load_rejection(self, sandbox_id: UUID, candidate: PolicyDecision) -> str | None:
        try:
            for name, expected_hash in candidate.index_hashes.items():
                content = self._artifacts.read(f"{sandbox_id}/{name}")
                if hashlib.sha256(content).hexdigest() != expected_hash:
                    return "CANDIDATE_LOAD_FAILED"
        except (OSError, ValueError):
            return "CANDIDATE_LOAD_FAILED"
        return None


def _offline_rejection(champion: PolicyDecision, candidate: PolicyDecision) -> str | None:
    selected = _selected_scorecard(candidate)
    current = _selected_scorecard(champion)
    if not selected.passed:
        return "QUALITY_GATE_FAILED"
    if selected.metrics.recall_at_10 < current.metrics.recall_at_10 - 0.02:
        return "RECALL_GATE_FAILED"
    if selected.metrics.ndcg_at_10 < current.metrics.ndcg_at_10 - 0.02:
        return "NDCG_GATE_FAILED"
    if selected.metrics.p95_latency_ms > 500:
        return "P95_LATENCY_GATE_FAILED"
    return None


def _selected_scorecard(decision: PolicyDecision) -> CandidateScorecard:
    return next(card for card in decision.scorecards if card.policy == decision.active_policy)


def _operational_rejection(observation: CanaryObservation) -> str | None:
    if observation.request_count < 100:
        return "INSUFFICIENT_CANARY_REQUESTS"
    if observation.load_failures > 0:
        return "CANDIDATE_LOAD_FAILED"
    if observation.availability < 0.99:
        return "AVAILABILITY_GATE_FAILED"
    if observation.error_rate > 0.01:
        return "ERROR_RATE_GATE_FAILED"
    if observation.p95_latency_ms > 500:
        return "P95_LATENCY_GATE_FAILED"
    if observation.fallback_rate > 0.01:
        return "FALLBACK_RATE_GATE_FAILED"
    return None
