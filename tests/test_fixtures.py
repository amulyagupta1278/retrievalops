import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from retrievalops.fixtures import FixtureManifest, validate_all_fixtures, validate_fixture

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_both_controlled_fixtures_have_valid_provenance_and_qrels() -> None:
    results = validate_all_fixtures(FIXTURES)

    assert [result.fixture_id for result in results] == [
        "government-schemes-pilot-v2-seed42",
        "technical-documentation-scifact-beir-test",
    ]
    assert results[0].positive_qrels == 183
    assert results[1].positive_qrels == 339


def test_fixture_validation_detects_content_drift(tmp_path: Path) -> None:
    source = FIXTURES / "government-schemes"
    fixture = tmp_path / "government-schemes"
    shutil.copytree(source, fixture)
    with (fixture / "corpus.jsonl").open("a", encoding="utf-8") as corpus:
        corpus.write("{}\n")

    with pytest.raises(ValueError, match="hash mismatch"):
        validate_fixture(fixture)


def test_unreviewed_evidence_status_is_rejected() -> None:
    payload = json.loads((FIXTURES / "government-schemes" / "manifest.json").read_text())
    payload["evidence_status"] = "ai_generated"

    with pytest.raises(ValidationError):
        FixtureManifest.model_validate(payload)
