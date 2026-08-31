from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from retrievalops.api import create_app
from retrievalops.config import Settings


def _blank_pdf(*, encrypted: bool = False) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("password")
    writer.write(output)
    return output.getvalue()


def test_valid_text_upload_creates_protected_queued_job(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/sandboxes",
            files={
                "file": (
                    "guide.txt",
                    b"RetrievalOps selects retrieval policies.",
                    "text/plain",
                )
            },
        )

        assert response.status_code == 202
        payload = response.json()
        assert payload["sandbox_id"]
        assert payload["sandbox_token"]
        assert payload["ingestion_job_id"]
        assert payload["status"] == "queued"
        assert payload["expires_at"].endswith("Z")
        assert app.state.metadata_store.token_matches(
            payload["sandbox_id"], payload["sandbox_token"]
        )
        assert not app.state.metadata_store.contains_token(payload["sandbox_token"])

    stored_files = list((tmp_path / "artifacts").rglob("source"))
    assert len(stored_files) == 1
    assert stored_files[0].read_bytes() == b"RetrievalOps selects retrieval policies."


def test_upload_ignores_path_components_in_filename(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/sandboxes",
            files={"file": ("../../guide.md", b"# Safe content", "text/markdown")},
        )

    assert response.status_code == 202
    assert not (tmp_path / "guide.md").exists()
    assert len(list((tmp_path / "artifacts").rglob("source"))) == 1


def test_spoofed_media_type_is_rejected_without_artifacts(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/sandboxes",
            files={"file": ("payload.pdf", b"not a pdf", "application/pdf")},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "INVALID_DOCUMENT", "message": "The PDF file is malformed."}
    }
    assert not list((tmp_path / "artifacts").rglob("source"))


def test_unsupported_extension_is_rejected(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/sandboxes",
            files={"file": ("payload.exe", b"hello", "application/octet-stream")},
        )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_DOCUMENT_TYPE"


def test_oversized_upload_is_rejected(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
            max_upload_bytes=16,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/sandboxes",
            files={"file": ("large.txt", b"a" * 17, "text/plain")},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "DOCUMENT_TOO_LARGE"
    assert not list((tmp_path / "artifacts").rglob("source"))


def test_blank_text_upload_is_rejected(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/sandboxes",
            files={"file": ("blank.txt", b" \n\t", "text/plain")},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EMPTY_DOCUMENT"


def test_scanned_or_empty_pdf_is_rejected(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/sandboxes",
            files={"file": ("scan.pdf", _blank_pdf(), "application/pdf")},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SCANNED_OR_EMPTY_PDF"


def test_encrypted_pdf_is_rejected(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            storage_root=tmp_path / "artifacts",
            database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/sandboxes",
            files={"file": ("secret.pdf", _blank_pdf(encrypted=True), "application/pdf")},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ENCRYPTED_PDF"
