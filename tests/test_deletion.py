from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from retrievalops.api import create_app
from retrievalops.config import Settings


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
        response = client.delete(
            f"/v1/sandboxes/{upload['sandbox_id']}",
            headers={"X-Sandbox-Token": upload["sandbox_token"]},
        )

        assert response.status_code == 204
        assert not app.state.metadata_store.token_matches(
            upload["sandbox_id"], upload["sandbox_token"]
        )
        assert app.state.metadata_store.deletion_audit_count(upload["sandbox_id"]) == 1
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
