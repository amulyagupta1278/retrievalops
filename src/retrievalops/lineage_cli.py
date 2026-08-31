import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from retrievalops.lineage import LineageRegistry, controlled_lineage


def main() -> None:
    parser = argparse.ArgumentParser(description="Register controlled RetrievalOps lineage")
    parser.add_argument("--tracking-uri", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=Path("evidence/controlled-benchmarks"))
    parser.add_argument("--commit-sha", default=_git_commit())
    parser.add_argument("--lock-file", type=Path, default=Path("uv.lock"))
    arguments = parser.parse_args()
    lock_hash = hashlib.sha256(arguments.lock_file.read_bytes()).hexdigest()
    registry = LineageRegistry(arguments.tracking_uri, arguments.artifact_root)
    registrations: list[dict[str, object]] = []
    for path in sorted(arguments.evidence.glob("*.json")):
        lineage = controlled_lineage(
            json.loads(path.read_text()),
            commit_sha=arguments.commit_sha,
            dependency_lock_hash=lock_hash,
        )
        registration = registry.register(lineage)
        reconstructed = registry.reconstruct(registration.registered_model_name)
        if reconstructed != lineage:
            raise RuntimeError("registered lineage could not be reconstructed exactly")
        registrations.append(registration.model_dump(mode="json"))
    print(json.dumps(registrations, indent=2))


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    main()
