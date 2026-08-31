import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from retrievalops.api import create_app
from retrievalops.config import Settings


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        storage_root=tmp_path / "artifacts",
        database_url=f"sqlite:///{tmp_path / 'metadata.db'}",
        **overrides,
    )


def test_public_evidence_page_labels_evidence_and_links_api(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert '<html lang="en">' in response.text
    assert "Controlled, human-reviewed evidence" in response.text
    assert "Synthetic/replay" in response.text
    assert "Live" in response.text
    assert 'href="/docs"' in response.text
    for evidence_path in sorted((Path(__file__).parents[1] / "evidence").glob("*/*.json")):
        if "controlled-benchmarks" not in str(evidence_path):
            continue
        run = json.loads(evidence_path.read_text())["run_2"]
        winner = next(card for card in run["scorecards"] if card["policy"] == run["active_policy"])
        assert f"{winner['metrics']['recall_at_10']:.4f}" in response.text
        assert f"{winner['metrics']['ndcg_at_10']:.4f}" in response.text


def test_api_docs_use_csp_compatible_same_origin_initialization(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        page = client.get("/docs")
        script = client.get("/docs-init.js")

    assert page.status_code == 200
    assert '<script src="/docs-init.js"></script>' in page.text
    assert "SwaggerUIBundle({" not in page.text
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("application/javascript")
    assert 'url: "/openapi.json"' in script.text


def test_security_headers_and_restricted_cors(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, allowed_origins=["https://demo.example"]))

    with TestClient(app) as client:
        response = client.get("/healthz")
        allowed = client.options(
            "/v1/sandboxes",
            headers={
                "Origin": "https://demo.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        denied = client.options(
            "/v1/sandboxes",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.headers["strict-transport-security"].startswith("max-age=31536000")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://demo.example"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_sandbox_rate_limit_is_isolated_and_returns_retry_after(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, sandbox_requests_per_minute=2))
    first_id = uuid4()
    second_id = uuid4()

    with TestClient(app) as client:
        first = client.delete(f"/v1/sandboxes/{first_id}", headers={"X-Sandbox-Token": "invalid"})
        second = client.delete(f"/v1/sandboxes/{first_id}", headers={"X-Sandbox-Token": "invalid"})
        limited = client.delete(f"/v1/sandboxes/{first_id}", headers={"X-Sandbox-Token": "invalid"})
        isolated = client.delete(
            f"/v1/sandboxes/{second_id}", headers={"X-Sandbox-Token": "different-invalid"}
        )

    assert first.status_code == second.status_code == isolated.status_code == 404
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert limited.json()["error"]["code"] == "SANDBOX_RATE_LIMITED"
