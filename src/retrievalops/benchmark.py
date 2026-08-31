import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QualityMetrics:
    recall_at_10: float
    ndcg_at_10: float
    mrr_at_10: float


def evaluate_rankings(
    rankings: Mapping[str, Sequence[str]],
    qrels: Mapping[str, Mapping[str, int]],
    *,
    k: int = 10,
) -> QualityMetrics:
    if not qrels:
        raise ValueError("at least one evaluated query is required")
    recalls: list[float] = []
    ndcgs: list[float] = []
    reciprocal_ranks: list[float] = []
    for query_id, judgments in qrels.items():
        relevant = {identifier: grade for identifier, grade in judgments.items() if grade > 0}
        if not relevant:
            raise ValueError(f"query {query_id} has no positive relevance judgment")
        retrieved = list(rankings.get(query_id, ()))[:k]
        relevant_retrieved = [identifier for identifier in retrieved if identifier in relevant]
        recalls.append(len(set(relevant_retrieved)) / len(relevant))
        first_rank = next(
            (rank for rank, identifier in enumerate(retrieved, start=1) if identifier in relevant),
            0,
        )
        reciprocal_ranks.append(1 / first_rank if first_rank else 0.0)
        dcg = sum(
            (2 ** relevant.get(identifier, 0) - 1) / math.log2(rank + 1)
            for rank, identifier in enumerate(retrieved, start=1)
        )
        ideal_grades = sorted(relevant.values(), reverse=True)[:k]
        ideal_dcg = sum(
            (2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal_grades, start=1)
        )
        ndcgs.append(dcg / ideal_dcg)
    return QualityMetrics(
        recall_at_10=sum(recalls) / len(recalls),
        ndcg_at_10=sum(ndcgs) / len(ndcgs),
        mrr_at_10=sum(reciprocal_ranks) / len(reciprocal_ranks),
    )
