import json
import os
from pathlib import Path

import pytest

from retrievalops.lineage import LineageRegistry, controlled_lineage


@pytest.mark.postgres
def test_registry_round_trip_against_postgres(tmp_path: Path) -> None:
    tracking_uri = os.getenv("RETRIEVALOPS_TEST_POSTGRES_URI")
    if not tracking_uri:
        pytest.skip("RETRIEVALOPS_TEST_POSTGRES_URI is not configured")
    root = Path(__file__).parents[1]
    evidence = json.loads(
        (
            root / "evidence" / "controlled-benchmarks" / "government-schemes-pilot-v2-seed42.json"
        ).read_text()
    )
    lineage = controlled_lineage(
        evidence,
        commit_sha="4a200cf1b198b7e3ff6f28d1d5f78f16ef2951c5",
        dependency_lock_hash="097f287cd5707d3033c8f2beda0887b71a98b32be4744f45b774a013c1eadf81",
    )
    registry = LineageRegistry(tracking_uri, tmp_path / "mlflow-artifacts")

    registration = registry.register(lineage)

    assert registry.reconstruct(registration.registered_model_name) == lineage
    assert registration.alias == "champion"
