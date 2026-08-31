import argparse
import json
from pathlib import Path

from retrievalops.controlled import compare_runs, run_controlled_fixture
from retrievalops.retrieval import SentenceTransformerEmbedder


def main() -> None:
    parser = argparse.ArgumentParser(description="Run reproducible controlled-fixture benchmarks")
    parser.add_argument("--fixtures", type=Path, default=Path("fixtures"))
    parser.add_argument("--output", type=Path, default=Path("evidence/controlled-benchmarks"))
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=True)
    embedder = SentenceTransformerEmbedder()
    summary: list[dict[str, object]] = []
    for fixture in sorted(path.parent for path in arguments.fixtures.glob("*/manifest.json")):
        first = run_controlled_fixture(fixture, embedder)
        second = run_controlled_fixture(fixture, embedder)
        report = compare_runs(first, second)
        payload = {
            "run_1": first.as_dict(),
            "run_2": second.as_dict(),
            "reproducibility": report.as_dict(),
        }
        destination = arguments.output / f"{first.fixture_id}.json"
        destination.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        summary.append(report.as_dict())
    print(json.dumps(summary, indent=2))
    if not all(bool(item["passed"]) for item in summary):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
