import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_runtime_image_is_pinned_offline_and_unprivileged() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert dockerfile.count("FROM python:3.12.14-slim-bookworm@sha256:") == 2
    assert "MODEL_REVISION=1110a243fdf4706b3f48f1d95db1a4f5529b4d41" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile


def test_ci_gates_publish_on_main_and_uses_immutable_action_revisions() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow_text = workflow_path.read_text()
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow["jobs"]

    assert jobs["publish"]["needs"] == ["quality", "container"]
    assert "github.event_name == 'push'" in jobs["publish"]["if"]
    assert "github.ref == 'refs/heads/main'" in jobs["publish"]["if"]
    assert "vars.PUBLIC_HOSTNAME != ''" in jobs["deploy"]["if"]
    assert "sha-${{ github.sha }}" in workflow_text
    assert "cosign sign --yes" in workflow_text
    assert "subject-digest: ${{ steps.build.outputs.digest }}" in workflow_text
    assert "RETRIEVALOPS_TEST_POSTGRES_URI" in workflow_text
    assert "retrievalops-benchmark-fixtures" in workflow_text
    assert "pip-audit==2.10.1" in workflow_text
    assert 'promtool" check rules deploy/observability/alerts.yml' in workflow_text

    action_uses = re.findall(r"uses:\s+([^\s]+)", workflow_text)
    assert action_uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in action_uses)
