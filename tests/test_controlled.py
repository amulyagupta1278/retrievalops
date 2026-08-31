import hashlib
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from retrievalops.controlled import compare_runs, run_controlled_fixture


class DeterministicEmbedder:
    model_name = "test-hash-embedder-v1"

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]:
        vectors = np.zeros((len(texts), 64), dtype=np.float32)
        for row, text in enumerate(texts):
            for term in text.casefold().split():
                vectors[row, hashlib.sha256(term.encode()).digest()[0] % 64] += 1
        return vectors


def test_government_fixture_rebuild_is_reproducible() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "government-schemes"
    first = run_controlled_fixture(fixture, DeterministicEmbedder())
    second = run_controlled_fixture(fixture, DeterministicEmbedder())
    report = compare_runs(first, second)

    assert report.passed is True
    assert all(report.checks.values())
    assert {card.policy for card in first.scorecards} == {"bm25", "dense", "hybrid"}
    assert first.index_hashes == second.index_hashes


def test_reproducibility_rejects_a_run_over_the_release_latency_gate() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "government-schemes"
    first = run_controlled_fixture(fixture, DeterministicEmbedder())
    second = run_controlled_fixture(fixture, DeterministicEmbedder())
    second.scorecards[0].metrics.p95_latency_ms = 500.01

    report = compare_runs(first, second)

    assert report.passed is False
    assert report.checks["latency_within_tolerance"] is False
