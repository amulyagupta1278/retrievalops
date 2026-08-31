from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
K8S = ROOT / "deploy" / "k8s"


def _document(path: str) -> dict[str, object]:
    payload = yaml.safe_load((K8S / path).read_text())
    assert isinstance(payload, dict)
    return payload


def test_application_rollout_preserves_capacity_and_uses_automatic_gates() -> None:
    rollout = _document("rollout.yaml")
    spec = rollout["spec"]
    canary = spec["strategy"]["canary"]

    assert spec["replicas"] >= 2
    assert canary["maxUnavailable"] == 0
    assert canary["stableService"] == "retrievalops-stable"
    assert canary["canaryService"] == "retrievalops-canary"
    assert [step.get("setWeight") for step in canary["steps"] if "setWeight" in step] == [
        10,
        50,
        100,
    ]
    assert len([step for step in canary["steps"] if "analysis" in step]) == 3
    container = spec["template"]["spec"]["containers"][0]
    assert "@sha256:" in container["image"]
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["resources"]["requests"]
    assert container["resources"]["limits"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_analysis_enforces_volume_availability_error_and_latency_gates() -> None:
    analysis = _document("analysis.yaml")
    metrics = {metric["name"]: metric for metric in analysis["spec"]["metrics"]}

    assert set(metrics) == {"request-volume", "availability", "error-rate", "p95-latency"}
    assert metrics["request-volume"]["successCondition"] == "result[0] >= 100"
    assert metrics["availability"]["successCondition"] == "result[0] >= 0.99"
    assert metrics["error-rate"]["successCondition"] == "result[0] <= 0.01"
    assert metrics["p95-latency"]["successCondition"] == "result[0] <= 0.5"
    assert all(metric["failureLimit"] == 1 for metric in metrics.values())


def test_release_workflow_waits_for_api_before_updating_worker() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    analysis_wait = workflow.index("status retrievalops -n retrievalops --timeout 15m")
    worker_update = workflow.index("deploy/k8s/worker.yaml")
    assert analysis_wait < worker_update
    assert "rollout undo deployment/retrievalops-worker" in workflow
    assert "champion" not in workflow
