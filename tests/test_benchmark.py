from retrievalops.benchmark import evaluate_rankings
from retrievalops.contracts import CandidateMetrics
from retrievalops.policy_compiler import compile_candidates


def _metrics(recall: float, ndcg: float, latency: float = 10) -> CandidateMetrics:
    return CandidateMetrics(
        recall_at_10=recall,
        ndcg_at_10=ndcg,
        mrr_at_10=ndcg,
        p50_latency_ms=latency,
        p95_latency_ms=latency,
        index_time_ms=1,
        estimated_cost_usd_per_1k_queries=0,
    )


def test_graded_metrics_handle_multiple_relevant_documents() -> None:
    metrics = evaluate_rankings(
        {"q1": ["d2", "irrelevant", "d1"]},
        {"q1": {"d1": 2, "d2": 1, "d3": 0}},
    )

    assert metrics.recall_at_10 == 1
    assert metrics.mrr_at_10 == 1
    assert 0 < metrics.ndcg_at_10 < 1


def test_compiler_records_quality_and_latency_rejections() -> None:
    scorecards, active, _reason = compile_candidates(
        {
            "bm25": _metrics(0.5, 0.5),
            "dense": _metrics(1, 1, latency=501),
            "hybrid": _metrics(1, 1, latency=20),
        },
        {"bm25": {"must-pass"}, "dense": set(), "hybrid": set()},
    )

    by_policy = {card.policy: card for card in scorecards}
    assert active == "hybrid"
    assert by_policy["bm25"].passed is False
    assert any("Recall@10" in reason for reason in by_policy["bm25"].rejection_reasons)
    assert any("must-pass" in reason for reason in by_policy["bm25"].rejection_reasons)
    assert by_policy["dense"].rejection_reasons == ["p95 retrieval latency exceeds 500 ms"]
