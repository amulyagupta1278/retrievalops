import csv
import hashlib
import json
import math
import time
from collections.abc import Sequence
from pathlib import Path
from statistics import median
from uuid import NAMESPACE_URL, uuid5

import faiss

from retrievalops.benchmark import evaluate_rankings
from retrievalops.contracts import CandidateMetrics, CandidateScorecard, Chunk
from retrievalops.fixtures import FixtureManifest, validate_fixture
from retrievalops.policy_compiler import POLICIES, ActivePolicyName, PolicyName, compile_candidates
from retrievalops.retrieval import (
    BM25Index,
    DenseIndex,
    Embedder,
    RankedChunk,
    reciprocal_rank_fusion,
)


class ControlledRun:
    def __init__(
        self,
        *,
        fixture_id: str,
        fixture_hash: str,
        configuration_hash: str,
        index_hashes: dict[str, str],
        scorecards: list[CandidateScorecard],
        active_policy: ActivePolicyName,
        compiler_reason: str,
    ) -> None:
        self.fixture_id = fixture_id
        self.fixture_hash = fixture_hash
        self.configuration_hash = configuration_hash
        self.index_hashes = index_hashes
        self.scorecards = scorecards
        self.active_policy = active_policy
        self.compiler_reason = compiler_reason

    def as_dict(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "fixture_hash": self.fixture_hash,
            "configuration_hash": self.configuration_hash,
            "index_hashes": self.index_hashes,
            "scorecards": [card.model_dump(mode="json") for card in self.scorecards],
            "active_policy": self.active_policy,
            "compiler_reason": self.compiler_reason,
        }


class ReproducibilityReport:
    def __init__(self, fixture_id: str, passed: bool, checks: dict[str, bool]) -> None:
        self.fixture_id = fixture_id
        self.passed = passed
        self.checks = checks

    def as_dict(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "passed": self.passed,
            "checks": self.checks,
            "tolerances": {
                "quality_metrics": "exact",
                "hashes_and_active_policy": "exact",
                "p95_latency_ms": "max(25 ms absolute, 50% relative)",
                "index_time_ms": "max(30000 ms absolute, 100% relative)",
            },
        }


def run_controlled_fixture(directory: Path, embedder: Embedder) -> ControlledRun:
    validation = validate_fixture(directory)
    manifest = FixtureManifest.model_validate_json((directory / "manifest.json").read_bytes())
    chunks, chunk_to_unit = _load_chunks(directory, manifest)
    queries = _load_queries(directory, manifest)
    qrels = _load_qrels(directory, manifest)
    evaluated_queries = {query_id: queries[query_id] for query_id in qrels}

    bm25_started = time.perf_counter()
    bm25 = BM25Index.build(chunks)
    bm25_time_ms = (time.perf_counter() - bm25_started) * 1_000
    dense_started = time.perf_counter()
    dense = DenseIndex.build(chunks, embedder)
    dense_time_ms = (time.perf_counter() - dense_started) * 1_000

    dense_identity = (
        faiss.serialize_index(dense.index).tobytes()
        + json.dumps(dense.chunk_ids, separators=(",", ":")).encode()
    )
    index_hashes = {
        "bm25": hashlib.sha256(bm25.to_bytes()).hexdigest(),
        "dense": hashlib.sha256(dense_identity).hexdigest(),
    }
    index_hashes["hybrid"] = hashlib.sha256(
        f"{index_hashes['bm25']}:{index_hashes['dense']}".encode()
    ).hexdigest()
    configuration = {
        "bm25": {"b": 0.75, "k1": 1.5},
        "chunk_overlap_tokens": 64,
        "chunk_tokens": 512,
        "dense_model": embedder.model_name,
        "hybrid": {"method": "rrf", "rank_constant": 60},
        "latency_reference": {
            "order": "query_id_ascending",
            "repetitions": 2,
            "sample_size": 20,
            "warmup_queries": 1,
        },
        "top_k": 10,
    }
    configuration_hash = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    rankings: dict[PolicyName, dict[str, list[str]]] = {policy: {} for policy in POLICIES}
    candidate_count = min(100, len(chunks))
    for query_id, query in evaluated_queries.items():
        lexical = bm25.search(query, candidate_count)
        semantic = dense.search(query, candidate_count, embedder)
        fused = reciprocal_rank_fusion([lexical, semantic], candidate_count)
        rankings["bm25"][query_id] = _collapse_units(lexical, chunk_to_unit, 10)
        rankings["dense"][query_id] = _collapse_units(semantic, chunk_to_unit, 10)
        rankings["hybrid"][query_id] = _collapse_units(fused, chunk_to_unit, 10)

    index_times = {
        "bm25": bm25_time_ms,
        "dense": dense_time_ms,
        "hybrid": bm25_time_ms + dense_time_ms,
    }
    latencies = _measure_reference_latency(
        evaluated_queries, bm25, dense, embedder, candidate_count
    )
    metrics: dict[PolicyName, CandidateMetrics] = {}
    misses: dict[PolicyName, set[str]] = {}
    for policy in POLICIES:
        quality = evaluate_rankings(rankings[policy], qrels)
        ordered = sorted(latencies[policy])
        p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
        metrics[policy] = CandidateMetrics(
            recall_at_10=quality.recall_at_10,
            ndcg_at_10=quality.ndcg_at_10,
            mrr_at_10=quality.mrr_at_10,
            p50_latency_ms=median(ordered),
            p95_latency_ms=ordered[p95_index],
            index_time_ms=index_times[policy],
            estimated_cost_usd_per_1k_queries=0.0,
        )
        misses[policy] = {
            query_id
            for query_id, judged in qrels.items()
            if not set(rankings[policy][query_id]).intersection(
                identifier for identifier, grade in judged.items() if grade == max(judged.values())
            )
        }
    scorecards, active_policy, reason = compile_candidates(metrics, misses)
    return ControlledRun(
        fixture_id=manifest.fixture_id,
        fixture_hash=validation.fixture_hash,
        configuration_hash=configuration_hash,
        index_hashes=index_hashes,
        scorecards=scorecards,
        active_policy=active_policy,
        compiler_reason=reason,
    )


def _measure_reference_latency(
    queries: dict[str, str],
    bm25: BM25Index,
    dense: DenseIndex,
    embedder: Embedder,
    candidate_count: int,
) -> dict[PolicyName, list[float]]:
    """Measure a fixed warmed workload independently from quality evaluation."""
    sample = [queries[query_id] for query_id in sorted(queries)[:20]]
    if not sample:
        raise ValueError("reference latency workload is empty")
    warm_lexical = bm25.search(sample[0], candidate_count)
    warm_semantic = dense.search(sample[0], candidate_count, embedder)
    reciprocal_rank_fusion([warm_lexical, warm_semantic], candidate_count)
    timings: dict[PolicyName, list[float]] = {policy: [] for policy in POLICIES}
    for query in sample:
        per_query: dict[PolicyName, list[float]] = {policy: [] for policy in POLICIES}
        for _repeat in range(2):
            started = time.perf_counter()
            lexical = bm25.search(query, candidate_count)
            lexical_ms = (time.perf_counter() - started) * 1_000
            per_query["bm25"].append(lexical_ms)
            started = time.perf_counter()
            semantic = dense.search(query, candidate_count, embedder)
            semantic_ms = (time.perf_counter() - started) * 1_000
            per_query["dense"].append(semantic_ms)
            started = time.perf_counter()
            reciprocal_rank_fusion([lexical, semantic], candidate_count)
            fusion_ms = (time.perf_counter() - started) * 1_000
            per_query["hybrid"].append(lexical_ms + semantic_ms + fusion_ms)
        for policy in POLICIES:
            timings[policy].append(median(per_query[policy]))
    return timings


def compare_runs(first: ControlledRun, second: ControlledRun) -> ReproducibilityReport:
    first_quality = {
        card.policy: (
            card.metrics.recall_at_10,
            card.metrics.ndcg_at_10,
            card.metrics.mrr_at_10,
        )
        for card in first.scorecards
    }
    second_quality = {
        card.policy: (
            card.metrics.recall_at_10,
            card.metrics.ndcg_at_10,
            card.metrics.mrr_at_10,
        )
        for card in second.scorecards
    }
    checks = {
        "fixture_hash_exact": first.fixture_hash == second.fixture_hash,
        "configuration_hash_exact": first.configuration_hash == second.configuration_hash,
        "index_hashes_exact": first.index_hashes == second.index_hashes,
        "quality_metrics_exact": first_quality == second_quality,
        "active_policy_exact": first.active_policy == second.active_policy,
        "latency_within_tolerance": _latency_within_tolerance(first, second),
        "index_time_within_tolerance": _index_time_within_tolerance(first, second),
    }
    return ReproducibilityReport(first.fixture_id, all(checks.values()), checks)


def _latency_within_tolerance(first: ControlledRun, second: ControlledRun) -> bool:
    second_cards = {card.policy: card for card in second.scorecards}
    for card in first.scorecards:
        first_p95 = card.metrics.p95_latency_ms
        second_p95 = second_cards[card.policy].metrics.p95_latency_ms
        if abs(first_p95 - second_p95) > max(25.0, first_p95 * 0.5):
            return False
    return True


def _index_time_within_tolerance(first: ControlledRun, second: ControlledRun) -> bool:
    second_cards = {card.policy: card for card in second.scorecards}
    for card in first.scorecards:
        first_time = card.metrics.index_time_ms
        second_time = second_cards[card.policy].metrics.index_time_ms
        if abs(first_time - second_time) > max(30_000.0, first_time):
            return False
    return True


def _load_chunks(directory: Path, manifest: FixtureManifest) -> tuple[list[Chunk], dict[str, str]]:
    chunks: list[Chunk] = []
    chunk_to_unit: dict[str, str] = {}
    path = directory / manifest.corpus.path
    for record in _json_lines(path):
        unit_id = str(record[manifest.corpus.id_field])
        title = (
            str(record.get(manifest.corpus.title_field, "")) if manifest.corpus.title_field else ""
        )
        text = "\n".join(
            part
            for part in (title.strip(), str(record[manifest.corpus.text_field]).strip())
            if part
        )
        words = text.split()
        starts = range(0, len(words), 448) if manifest.corpus_unit == "document" else (0,)
        for ordinal, start in enumerate(starts):
            window = words[start : start + 512]
            if not window:
                continue
            chunk_id = unit_id if manifest.corpus_unit == "passage" else f"{unit_id}::{ordinal:04d}"
            chunk_text = " ".join(window)
            chunks.append(
                Chunk(
                    id=chunk_id,
                    document_id=uuid5(NAMESPACE_URL, f"{manifest.fixture_id}:{unit_id}"),
                    ordinal=ordinal,
                    text=chunk_text,
                    token_count=len(window),
                    sha256=hashlib.sha256(chunk_text.encode()).hexdigest(),
                )
            )
            chunk_to_unit[chunk_id] = unit_id
            if len(window) < 512:
                break
    return chunks, chunk_to_unit


def _load_queries(directory: Path, manifest: FixtureManifest) -> dict[str, str]:
    return {
        str(record[manifest.queries.id_field]): str(record[manifest.queries.text_field])
        for record in _json_lines(directory / manifest.queries.path)
    }


def _load_qrels(directory: Path, manifest: FixtureManifest) -> dict[str, dict[str, int]]:
    spec = manifest.qrels
    qrels: dict[str, dict[str, int]] = {}
    with (directory / spec.path).open(encoding="utf-8", newline="") as source:
        rows = csv.reader(source, delimiter="\t")
        if spec.has_header:
            next(rows)
        for row in rows:
            score = int(row[spec.score_column])
            query_id = row[spec.query_id_column]
            corpus_id = row[spec.corpus_id_column]
            qrels.setdefault(query_id, {})[corpus_id] = score
    return qrels


def _json_lines(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source]


def _collapse_units(
    ranking: Sequence[RankedChunk], chunk_to_unit: dict[str, str], k: int
) -> list[str]:
    units: list[str] = []
    for item in ranking:
        chunk_id = item.chunk_id
        unit = chunk_to_unit[chunk_id]
        if unit not in units:
            units.append(unit)
        if len(units) == k:
            break
    return units
