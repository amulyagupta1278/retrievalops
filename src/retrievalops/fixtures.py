import csv
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from retrievalops.contracts import Sha256


class JsonLinesSpec(BaseModel):
    path: str
    sha256: Sha256
    id_field: str
    text_field: str
    title_field: str | None = None
    expected_records: Annotated[int, Field(gt=0)]


class QrelsSpec(BaseModel):
    path: str
    sha256: Sha256
    has_header: bool
    query_id_column: Annotated[int, Field(ge=0)]
    corpus_id_column: Annotated[int, Field(ge=0)]
    score_column: Annotated[int, Field(ge=0)]
    expected_records: Annotated[int, Field(gt=0)]


class FileSpec(BaseModel):
    path: str
    sha256: Sha256


class FixtureManifest(BaseModel):
    schema_version: Literal[1]
    fixture_id: str
    domain: str
    corpus_unit: Literal["passage", "document"]
    source_url: str
    source_revision: str
    license: str
    evidence_status: Literal["owner_adjudicated", "expert_annotated"]
    evidence_provenance: str
    provenance_files: Annotated[list[FileSpec], Field(min_length=1)]
    corpus: JsonLinesSpec
    queries: JsonLinesSpec
    qrels: QrelsSpec


class FixtureValidation(BaseModel):
    fixture_id: str
    corpus_records: int
    query_records: int
    qrel_records: int
    positive_qrels: int
    fixture_hash: Sha256


def validate_fixture(directory: Path) -> FixtureValidation:
    manifest_path = directory / "manifest.json"
    manifest_content = manifest_path.read_bytes()
    manifest = FixtureManifest.model_validate_json(manifest_content)
    for provenance_file in manifest.provenance_files:
        _verify_hash(_fixture_path(directory, provenance_file.path), provenance_file.sha256)
    corpus_ids = _validate_json_lines(directory, manifest.corpus)
    query_ids = _validate_json_lines(directory, manifest.queries)
    qrel_records, positive_qrels = _validate_qrels(directory, manifest.qrels, query_ids, corpus_ids)
    fixture_hash = hashlib.sha256(
        manifest_content
        + manifest.corpus.sha256.encode()
        + manifest.queries.sha256.encode()
        + manifest.qrels.sha256.encode()
        + "".join(file.sha256 for file in manifest.provenance_files).encode()
    ).hexdigest()
    return FixtureValidation(
        fixture_id=manifest.fixture_id,
        corpus_records=len(corpus_ids),
        query_records=len(query_ids),
        qrel_records=qrel_records,
        positive_qrels=positive_qrels,
        fixture_hash=fixture_hash,
    )


def validate_all_fixtures(root: Path) -> list[FixtureValidation]:
    directories = sorted(path.parent for path in root.glob("*/manifest.json"))
    if len(directories) != 2:
        raise ValueError("exactly two controlled fixtures are required")
    results = [validate_fixture(directory) for directory in directories]
    identifiers = {result.fixture_id for result in results}
    if len(identifiers) != len(results):
        raise ValueError("fixture identifiers must be unique")
    return results


def _validate_json_lines(directory: Path, spec: JsonLinesSpec) -> set[str]:
    path = _fixture_path(directory, spec.path)
    _verify_hash(path, spec.sha256)
    identifiers: set[str] = set()
    with path.open(encoding="utf-8") as records:
        for line_number, line in enumerate(records, start=1):
            try:
                record = json.loads(line)
                identifier = str(record[spec.id_field]).strip()
                text = str(record[spec.text_field]).strip()
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                raise ValueError(f"invalid JSONL record at {path}:{line_number}") from error
            if not identifier or not text:
                raise ValueError(f"blank identifier or text at {path}:{line_number}")
            if identifier in identifiers:
                raise ValueError(f"duplicate identifier {identifier} in {path}")
            identifiers.add(identifier)
    if len(identifiers) != spec.expected_records:
        raise ValueError(f"record count mismatch for {path}")
    return identifiers


def _validate_qrels(
    directory: Path, spec: QrelsSpec, query_ids: set[str], corpus_ids: set[str]
) -> tuple[int, int]:
    path = _fixture_path(directory, spec.path)
    _verify_hash(path, spec.sha256)
    count = 0
    positives = 0
    seen: set[tuple[str, str]] = set()
    required_column = max(spec.query_id_column, spec.corpus_id_column, spec.score_column)
    with path.open(encoding="utf-8", newline="") as source:
        rows = csv.reader(source, delimiter="\t")
        if spec.has_header:
            next(rows, None)
        for line_number, row in enumerate(rows, start=2 if spec.has_header else 1):
            if len(row) <= required_column:
                raise ValueError(f"invalid qrel at {path}:{line_number}")
            query_id = row[spec.query_id_column].strip()
            corpus_id = row[spec.corpus_id_column].strip()
            try:
                score = int(row[spec.score_column])
            except ValueError as error:
                raise ValueError(f"invalid qrel score at {path}:{line_number}") from error
            if query_id not in query_ids or corpus_id not in corpus_ids or score < 0:
                raise ValueError(f"orphaned or negative qrel at {path}:{line_number}")
            pair = (query_id, corpus_id)
            if pair in seen:
                raise ValueError(f"duplicate qrel pair at {path}:{line_number}")
            seen.add(pair)
            count += 1
            positives += int(score > 0)
    if count != spec.expected_records or positives == 0:
        raise ValueError(f"qrel count or positive-evidence mismatch for {path}")
    return count, positives


def _fixture_path(directory: Path, relative_path: str) -> Path:
    path = (directory / relative_path).resolve()
    if directory.resolve() not in path.parents:
        raise ValueError("fixture path escapes its directory")
    return path


def _verify_hash(path: Path, expected: str) -> None:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"hash mismatch for {path}")
