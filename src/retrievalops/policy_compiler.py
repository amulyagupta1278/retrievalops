from collections.abc import Mapping, Set
from typing import Literal

from retrievalops.contracts import CandidateMetrics, CandidateScorecard

PolicyName = Literal["bm25", "dense", "hybrid"]
ActivePolicyName = PolicyName | Literal["bootstrap-hybrid"]
POLICIES: tuple[PolicyName, ...] = ("bm25", "dense", "hybrid")


def compile_candidates(
    metrics_by_policy: Mapping[PolicyName, CandidateMetrics],
    must_pass_misses: Mapping[PolicyName, Set[str]],
) -> tuple[list[CandidateScorecard], ActivePolicyName, str]:
    if set(metrics_by_policy) != set(POLICIES) or set(must_pass_misses) != set(POLICIES):
        raise ValueError("compiler requires exactly BM25, dense, and hybrid evidence")
    baseline = metrics_by_policy["hybrid"]
    scorecards: list[CandidateScorecard] = []
    for policy in POLICIES:
        metrics = metrics_by_policy[policy]
        reasons: list[str] = []
        if metrics.recall_at_10 < baseline.recall_at_10 - 0.02:
            reasons.append("Recall@10 regressed by more than 0.02 versus bootstrap hybrid")
        if metrics.ndcg_at_10 < baseline.ndcg_at_10 - 0.02:
            reasons.append("nDCG@10 regressed by more than 0.02 versus bootstrap hybrid")
        if must_pass_misses[policy] - must_pass_misses["hybrid"]:
            reasons.append("One or more must-pass judgments regressed versus bootstrap hybrid")
        if metrics.p95_latency_ms > 500:
            reasons.append("p95 retrieval latency exceeds 500 ms")
        scorecards.append(
            CandidateScorecard(
                policy=policy,
                metrics=metrics,
                passed=not reasons,
                rejection_reasons=reasons,
            )
        )
    passing = [card for card in scorecards if card.passed]
    if not passing:
        return (
            scorecards,
            "bootstrap-hybrid",
            "No candidate passed every hard gate; retained bootstrap hybrid.",
        )
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
    return (
        scorecards,
        winner.policy,
        "Selected the highest passing nDCG@10, then Recall@10, lower p95 latency, "
        "lower estimated cost, and policy name.",
    )
