import hashlib
import json
import math
import re
from statistics import median
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid5

from retrievalops.contracts import (
    CandidateMetrics,
    CandidateScorecard,
    Chunk,
    EvaluationSuggestion,
    Judgment,
    PolicyDecision,
)
from retrievalops.querying import QueryService
from retrievalops.storage import ArtifactStore

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]{3,}")
PolicyName = Literal["bm25", "dense", "hybrid"]
_POLICIES: tuple[PolicyName, ...] = ("bm25", "dense", "hybrid")
_QUESTION_TEMPLATES = (
    "What does the document say about {topic}?",
    "How does the document describe {topic}?",
    "Which details are provided about {topic}?",
    "Why is {topic} important in this document?",
    "Summarize the document's guidance on {topic}.",
)


class EvaluationService:
    def __init__(self, artifacts: ArtifactStore, querying: QueryService) -> None:
        self._artifacts = artifacts
        self._querying = querying

    def chunks(self, sandbox_id: UUID) -> list[Chunk]:
        content = self._artifacts.read(f"{sandbox_id}/chunks.json")
        return [Chunk.model_validate(item) for item in json.loads(content)]

    def suggestions(self, sandbox_id: UUID) -> list[EvaluationSuggestion]:
        chunks = self.chunks(sandbox_id)
        suggestions: list[EvaluationSuggestion] = []
        for ordinal in range(5):
            chunk = chunks[ordinal % len(chunks)]
            terms = list(dict.fromkeys(term.casefold() for term in _WORD.findall(chunk.text)))
            start = (ordinal * 3) % max(len(terms), 1)
            selected_terms = (terms + terms)[start : start + 3]
            topic = ", ".join(selected_terms) or f"passage {chunk.ordinal + 1}"
            suggestions.append(
                EvaluationSuggestion(
                    id=uuid5(sandbox_id, f"suggestion:{ordinal}:{chunk.sha256}"),
                    query=_QUESTION_TEMPLATES[ordinal].format(topic=topic),
                    relevant_chunk_id=chunk.id,
                    passage=chunk.text,
                )
            )
        return suggestions

    def optimize(self, sandbox_id: UUID, judgments: list[Judgment]) -> PolicyDecision:
        evidence = [
            {
                "query": judgment.query,
                "relevant_chunk_id": judgment.relevant_chunk_id,
                "relevance": judgment.relevance,
            }
            for judgment in sorted(
                judgments,
                key=lambda item: (item.query, item.relevant_chunk_id, item.relevance),
            )
        ]
        evidence_content = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        evidence_hash = hashlib.sha256(evidence_content).hexdigest()
        try:
            existing = json.loads(self._artifacts.read(f"{sandbox_id}/active_policy.json"))
            if existing["evidence_hash"] == evidence_hash:
                return PolicyDecision.model_validate(existing)
        except FileNotFoundError:
            pass

        manifest = json.loads(self._artifacts.read(f"{sandbox_id}/index_manifest.json"))
        benchmark_results = {
            policy: self._benchmark(
                sandbox_id, policy, judgments, manifest["index_time_ms"][policy]
            )
            for policy in _POLICIES
        }
        raw_metrics = {policy: result[0] for policy, result in benchmark_results.items()}
        missed_must_pass = {policy: result[1] for policy, result in benchmark_results.items()}
        baseline = raw_metrics["hybrid"]
        scorecards: list[CandidateScorecard] = []
        for policy in _POLICIES:
            metrics = raw_metrics[policy]
            reasons: list[str] = []
            if metrics.recall_at_10 < baseline.recall_at_10 - 0.02:
                reasons.append("Recall@10 regressed by more than 0.02 versus bootstrap hybrid")
            if metrics.ndcg_at_10 < baseline.ndcg_at_10 - 0.02:
                reasons.append("nDCG@10 regressed by more than 0.02 versus bootstrap hybrid")
            new_must_pass_misses = missed_must_pass[policy] - missed_must_pass["hybrid"]
            if new_must_pass_misses:
                reasons.append("One or more must-pass judgments regressed versus bootstrap hybrid")
            if metrics.p95_latency_ms > 500:
                reasons.append("p95 retrieval latency exceeds 500 ms")
            scorecards.append(
                CandidateScorecard(
                    policy=policy, metrics=metrics, passed=not reasons, rejection_reasons=reasons
                )
            )
        passing = [card for card in scorecards if card.passed]
        if passing:
            winner = sorted(
                passing,
                key=lambda card: (
                    -card.metrics.ndcg_at_10,
                    -card.metrics.recall_at_10,
                    card.metrics.p95_latency_ms,
                    card.metrics.estimated_cost_usd_per_1k_queries,
                    card.policy,
                ),
            )[0]
            active_policy: PolicyName | Literal["bootstrap-hybrid"] = winner.policy
            compiler_reason = (
                "Selected the highest passing nDCG@10, then Recall@10, lower p95 latency, "
                "lower estimated cost, and policy name."
            )
        else:
            active_policy = "bootstrap-hybrid"
            compiler_reason = "No candidate passed every hard gate; retained bootstrap hybrid."
        version_material = json.dumps(
            {
                "active_policy": active_policy,
                "configuration_sha256": manifest["configuration_sha256"],
                "evidence_hash": evidence_hash,
                "files": manifest["files"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        policy_version = hashlib.sha256(version_material).hexdigest()[:16]
        decision = PolicyDecision(
            active_policy=active_policy,
            policy_version=policy_version,
            evidence_hash=evidence_hash,
            corpus_hash=manifest["document_sha256"],
            configuration_hash=manifest["configuration_sha256"],
            index_hashes=manifest["files"],
            scorecards=scorecards,
            compiler_reason=compiler_reason,
        )
        payload = decision.model_dump(mode="json")
        self._artifacts.write_immutable_json(sandbox_id, f"policy-{policy_version}.json", payload)
        self._artifacts.write_json(sandbox_id, "active_policy.json", payload)
        return decision

    def _benchmark(
        self,
        sandbox_id: UUID,
        policy: PolicyName,
        judgments: list[Judgment],
        index_time_ms: float,
    ) -> tuple[CandidateMetrics, set[str]]:
        recalls: list[float] = []
        reciprocal_ranks: list[float] = []
        ndcgs: list[float] = []
        latencies: list[float] = []
        missed_must_pass: set[str] = set()
        for judgment in judgments:
            started = perf_counter()
            hits = self._querying.search_policy(sandbox_id, judgment.query, 10, policy)
            latencies.append((perf_counter() - started) * 1_000)
            identifiers = [hit.chunk.id for hit in hits]
            rank = (
                identifiers.index(judgment.relevant_chunk_id) + 1
                if judgment.relevant_chunk_id in identifiers
                else 0
            )
            recalls.append(float(rank > 0))
            reciprocal_ranks.append(1 / rank if rank else 0.0)
            ndcgs.append(1 / math.log2(rank + 1) if rank else 0.0)
            if judgment.relevance == 3 and not rank:
                missed_must_pass.add(str(judgment.id))
        ordered_latency = sorted(latencies)
        p95_index = max(0, math.ceil(0.95 * len(ordered_latency)) - 1)
        return (
            CandidateMetrics(
                recall_at_10=sum(recalls) / len(recalls),
                ndcg_at_10=sum(ndcgs) / len(ndcgs),
                mrr_at_10=sum(reciprocal_ranks) / len(reciprocal_ranks),
                p50_latency_ms=median(ordered_latency),
                p95_latency_ms=ordered_latency[p95_index],
                index_time_ms=index_time_ms,
                estimated_cost_usd_per_1k_queries=0.0,
            ),
            missed_must_pass,
        )
