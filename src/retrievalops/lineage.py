import hashlib
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal, Self

from mlflow import MlflowClient
from mlflow.entities.model_registry import ModelVersion
from mlflow.exceptions import MlflowException
from pydantic import BaseModel, Field, model_validator

from retrievalops.contracts import CandidateScorecard, PolicyDecision, Sha256

CommitDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{7,64}$")]
_SAFE_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_.-]+")


class LineageRecord(BaseModel):
    schema_version: Literal[1] = 1
    scope: Literal["controlled", "ephemeral"]
    subject_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")]
    policy_name: Literal["bootstrap-hybrid", "bm25", "dense", "hybrid"]
    policy_hash: Sha256
    dataset_hash: Sha256
    evidence_hash: Sha256
    configuration_hash: Sha256
    index_hashes: Annotated[dict[str, Sha256], Field(min_length=3)]
    commit_sha: CommitDigest
    dependency_lock_hash: Sha256
    scorecards: Annotated[list[CandidateScorecard], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def exact_candidate_set(self) -> Self:
        if {scorecard.policy for scorecard in self.scorecards} != {
            "bm25",
            "dense",
            "hybrid",
        }:
            raise ValueError("lineage requires all three candidate scorecards")
        return self

    @property
    def lineage_hash(self) -> str:
        return hashlib.sha256(_canonical(self.model_dump(mode="json"))).hexdigest()


class RegisteredLineage(BaseModel):
    experiment_name: str
    run_id: str
    registered_model_name: str
    model_version: str
    alias: Literal["champion", "candidate"] = "champion"
    lineage_hash: Sha256


class LineageRegistry:
    def __init__(self, tracking_uri: str, artifact_root: Path | None = None) -> None:
        self._client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
        self._artifact_root = artifact_root

    def register(
        self, lineage: LineageRecord, *, alias: Literal["champion", "candidate"] = "champion"
    ) -> RegisteredLineage:
        experiment_name = f"retrievalops-{lineage.scope}"
        model_name = self._model_name(lineage)
        existing = self._existing_version(model_name, lineage.lineage_hash)
        if existing is not None:
            if not existing.run_id:
                raise ValueError("existing registered version has no source run")
            self._client.set_registered_model_alias(model_name, alias, existing.version)
            return RegisteredLineage(
                experiment_name=experiment_name,
                run_id=str(existing.run_id),
                registered_model_name=model_name,
                model_version=str(existing.version),
                alias=alias,
                lineage_hash=lineage.lineage_hash,
            )
        experiment = self._client.get_experiment_by_name(experiment_name)
        experiment_id = (
            experiment.experiment_id
            if experiment is not None
            else self._client.create_experiment(
                experiment_name,
                artifact_location=self._artifact_location(lineage.scope),
                tags={"retrievalops.scope": lineage.scope},
            )
        )
        run = self._client.create_run(
            experiment_id,
            tags={
                "retrievalops.scope": lineage.scope,
                "retrievalops.subject_id": lineage.subject_id,
                "retrievalops.lineage_hash": lineage.lineage_hash,
            },
            run_name=f"{lineage.subject_id}-{lineage.policy_name}",
        )
        try:
            self._log_lineage(run.info.run_id, lineage)
            self._ensure_registered_model(model_name, lineage.scope)
            version = self._client.create_model_version(
                name=model_name,
                source=f"{run.info.artifact_uri}/policy_bundle",
                run_id=run.info.run_id,
                tags={
                    "lineage_hash": lineage.lineage_hash,
                    "policy_hash": lineage.policy_hash,
                    "dataset_hash": lineage.dataset_hash,
                    "configuration_hash": lineage.configuration_hash,
                    "commit_sha": lineage.commit_sha,
                    "dependency_lock_hash": lineage.dependency_lock_hash,
                },
                description="Retrieval policy metadata bundle; no corpus or query text.",
            )
            self._client.set_registered_model_alias(model_name, alias, version.version)
            self._client.set_terminated(run.info.run_id, "FINISHED")
        except Exception:
            self._client.set_terminated(run.info.run_id, "FAILED")
            raise
        return RegisteredLineage(
            experiment_name=experiment_name,
            run_id=run.info.run_id,
            registered_model_name=model_name,
            model_version=str(version.version),
            alias=alias,
            lineage_hash=lineage.lineage_hash,
        )

    def reconstruct(self, model_name: str, alias: str = "champion") -> LineageRecord:
        version = self._client.get_model_version_by_alias(model_name, alias)
        if not version.run_id:
            raise ValueError("registered version has no source run")
        with TemporaryDirectory() as destination:
            path = self._client.download_artifacts(
                version.run_id, "policy_bundle/lineage.json", destination
            )
            lineage = LineageRecord.model_validate_json(Path(path).read_bytes())
        if version.tags.get("lineage_hash") != lineage.lineage_hash:
            raise ValueError("registered lineage hash does not match the source artifact")
        expected_tags = {
            "policy_hash": lineage.policy_hash,
            "dataset_hash": lineage.dataset_hash,
            "configuration_hash": lineage.configuration_hash,
            "commit_sha": lineage.commit_sha,
            "dependency_lock_hash": lineage.dependency_lock_hash,
        }
        if any(version.tags.get(key) != value for key, value in expected_tags.items()):
            raise ValueError("registered version tags do not match the source artifact")
        return lineage

    def promote_ephemeral_candidate(self, subject_id: str) -> None:
        subject = _SAFE_IDENTIFIER.sub("-", subject_id).strip("-.")
        if not subject:
            raise ValueError("subject identifier has no registry-safe characters")
        model_name = f"retrievalops.ephemeral.{subject}"
        candidate = self._client.get_model_version_by_alias(model_name, "candidate")
        self._client.set_registered_model_alias(model_name, "champion", candidate.version)

    def _log_lineage(self, run_id: str, lineage: LineageRecord) -> None:
        payload = lineage.model_dump(mode="json")
        self._client.log_dict(run_id, payload, "policy_bundle/lineage.json")
        self._client.log_dict(
            run_id,
            {
                "policy_name": lineage.policy_name,
                "policy_hash": lineage.policy_hash,
                "lineage_hash": lineage.lineage_hash,
            },
            "policy_bundle/policy.json",
        )
        for key, value in {
            "dataset_hash": lineage.dataset_hash,
            "evidence_hash": lineage.evidence_hash,
            "configuration_hash": lineage.configuration_hash,
            "commit_sha": lineage.commit_sha,
            "dependency_lock_hash": lineage.dependency_lock_hash,
        }.items():
            self._client.log_param(run_id, key, value)
        for scorecard in lineage.scorecards:
            for metric, value in scorecard.metrics.model_dump().items():
                self._client.log_metric(run_id, f"{scorecard.policy}.{metric}", float(value))

    def _ensure_registered_model(self, name: str, scope: str) -> None:
        try:
            self._client.get_registered_model(name)
        except MlflowException as error:
            if error.error_code != "RESOURCE_DOES_NOT_EXIST":
                raise
            try:
                self._client.create_registered_model(
                    name,
                    tags={"retrievalops.scope": scope},
                    description="Versioned RetrievalOps policy bundles.",
                )
            except MlflowException as create_error:
                if create_error.error_code != "RESOURCE_ALREADY_EXISTS":
                    raise
                self._client.get_registered_model(name)

    def _existing_version(self, model_name: str, lineage_hash: str) -> ModelVersion | None:
        try:
            versions = self._client.search_model_versions(f"name = '{model_name}'")
        except MlflowException:
            return None
        return next(
            (version for version in versions if version.tags.get("lineage_hash") == lineage_hash),
            None,
        )

    @staticmethod
    def _model_name(lineage: LineageRecord) -> str:
        subject = _SAFE_IDENTIFIER.sub("-", lineage.subject_id).strip("-.")
        if not subject:
            raise ValueError("subject identifier has no registry-safe characters")
        return f"retrievalops.{lineage.scope}.{subject}"

    def _artifact_location(self, scope: str) -> str | None:
        if self._artifact_root is None:
            return None
        path = (self._artifact_root / scope).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path.as_uri()


def ephemeral_lineage(
    decision: PolicyDecision,
    *,
    sandbox_id: str,
    commit_sha: str,
    dependency_lock_hash: str,
) -> LineageRecord:
    payload = decision.model_dump(mode="json")
    return LineageRecord(
        scope="ephemeral",
        subject_id=sandbox_id,
        policy_name=decision.active_policy,
        policy_hash=hashlib.sha256(_canonical(payload)).hexdigest(),
        dataset_hash=decision.corpus_hash,
        evidence_hash=decision.evidence_hash,
        configuration_hash=decision.configuration_hash,
        index_hashes=decision.index_hashes,
        commit_sha=commit_sha,
        dependency_lock_hash=dependency_lock_hash,
        scorecards=decision.scorecards,
    )


def controlled_lineage(
    evidence: dict[str, object],
    *,
    commit_sha: str,
    dependency_lock_hash: str,
) -> LineageRecord:
    run = evidence.get("run_2")
    if not isinstance(run, dict):
        raise ValueError("controlled evidence has no second reproducible run")
    reproducibility = evidence.get("reproducibility")
    if not isinstance(reproducibility, dict) or reproducibility.get("passed") is not True:
        raise ValueError("controlled evidence did not pass reproducibility gates")
    scorecards = [CandidateScorecard.model_validate(item) for item in run["scorecards"]]
    policy_hash = hashlib.sha256(_canonical(run)).hexdigest()
    return LineageRecord.model_validate(
        {
            "scope": "controlled",
            "subject_id": run["fixture_id"],
            "policy_name": run["active_policy"],
            "policy_hash": policy_hash,
            "dataset_hash": run["fixture_hash"],
            "evidence_hash": run["fixture_hash"],
            "configuration_hash": run["configuration_hash"],
            "index_hashes": run["index_hashes"],
            "commit_sha": commit_sha,
            "dependency_lock_hash": dependency_lock_hash,
            "scorecards": scorecards,
        }
    )


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
