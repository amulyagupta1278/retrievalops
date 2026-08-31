from fastapi.testclient import TestClient

from retrievalops.api import app


def test_health_reports_service_and_build_identity() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "retrievalops",
        "version": "0.1.0",
        "build_sha": "development",
    }
