import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from retrievalops.api import create_app
from retrievalops.cleanup_cli import main as cleanup_main
from retrievalops.config import Settings, get_settings
from retrievalops.contracts import Document, IngestionJob, JobState, Judgment, Sandbox
from retrievalops.metadata import MetadataStore
from retrievalops.storage import ArtifactStore


def _app(tmp_path: Path) -> FastAPI:
    return create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        )
    )


def _upload(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/v1/sandboxes",
        files={"file": ("guide.txt", b"RetrievalOps deletion evidence.", "text/plain")},
    )
    assert response.status_code == 202
    return response.json()


def test_owner_can_delete_sandbox_and_content(tmp_path: Path) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        upload = _upload(client)
        sandbox_id = UUID(upload["sandbox_id"])
        app.state.metadata_store.replace_judgments(
            sandbox_id,
            [
                Judgment(
                    id=uuid4(),
                    sandbox_id=sandbox_id,
                    query="What must deletion remove?",
                    relevant_chunk_id=f"{sandbox_id}:fixture:000000",
                    relevance=3,
                    reviewed=True,
                )
            ],
        )
        response = client.delete(
            f"/v1/sandboxes/{upload['sandbox_id']}",
            headers={"X-Sandbox-Token": upload["sandbox_token"]},
        )

        assert response.status_code == 204
        assert not app.state.metadata_store.token_matches(
            upload["sandbox_id"], upload["sandbox_token"]
        )
        assert app.state.metadata_store.deletion_audit_count(upload["sandbox_id"]) == 1
        assert app.state.metadata_store.judgments(sandbox_id) == []
        assert not (tmp_path / "artifacts" / upload["sandbox_id"]).exists()


def test_invalid_token_cannot_delete_sandbox(tmp_path: Path) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        upload = _upload(client)
        response = client.delete(
            f"/v1/sandboxes/{upload['sandbox_id']}",
            headers={"X-Sandbox-Token": "wrong-token"},
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "SANDBOX_NOT_FOUND"
        assert (tmp_path / "artifacts" / upload["sandbox_id"] / "source").exists()


def test_cleanup_deletes_expired_sandboxes_idempotently(tmp_path: Path) -> None:
    app = _app(tmp_path)

    with TestClient(app) as client:
        upload = _upload(client)
        future = datetime.now(UTC) + timedelta(hours=25)

        assert app.state.sandbox_lifecycle.cleanup_expired(future) == 1
        assert app.state.sandbox_lifecycle.cleanup_expired(future) == 0
        assert app.state.metadata_store.deletion_audit_count(upload["sandbox_id"]) == 1
        assert not (tmp_path / "artifacts" / upload["sandbox_id"]).exists()


def test_cleanup_command_deletes_expired_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "metadata.db"
    artifacts = tmp_path / "artifacts"
    database_url = f"sqlite:///{database}"
    metadata = MetadataStore(database_url)
    metadata.initialize()
    artifact_store = ArtifactStore(artifacts)
    sandbox_id = uuid4()
    now = datetime.now(UTC)
    source = b"expired public content"
    storage_key = artifact_store.write_source(sandbox_id, source)
    metadata.create_upload(
        Sandbox(
            id=sandbox_id,
            created_at=now - timedelta(hours=26),
            expires_at=now - timedelta(hours=2),
        ),
        Document(
            id=uuid4(),
            sandbox_id=sandbox_id,
            filename="expired.txt",
            media_type="text/plain",
            size_bytes=len(source),
            sha256=hashlib.sha256(source).hexdigest(),
        ),
        IngestionJob(id=uuid4(), sandbox_id=sandbox_id, state=JobState.queued),
        "one-time-token",
        storage_key,
    )
    monkeypatch.setenv("RETRIEVALOPS_DATABASE_URL", database_url)
    monkeypatch.setenv("RETRIEVALOPS_STORAGE_ROOT", str(artifacts))
    get_settings.cache_clear()
    try:
        cleanup_main()
    finally:
        get_settings.cache_clear()

    assert capsys.readouterr().out == '{"deleted_sandboxes": 1}\n'
    assert not (artifacts / str(sandbox_id)).exists()
    assert metadata.deletion_audit_count(sandbox_id) == 1
