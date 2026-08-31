from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
K8S = ROOT / "deploy" / "k8s"


def _document(path: str) -> dict[str, object]:
    payload = yaml.safe_load((K8S / path).read_text())
    assert isinstance(payload, dict)
    return payload


def _documents(path: str) -> list[dict[str, object]]:
    payloads = list(yaml.safe_load_all((K8S / path).read_text()))
    assert all(isinstance(payload, dict) for payload in payloads)
    return payloads


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


def test_public_ingress_enforces_https_ip_rate_and_concurrency_limits() -> None:
    resources = _documents("traefik.yaml")
    by_name = {resource["metadata"]["name"]: resource for resource in resources}

    secure = by_name["retrievalops"]
    assert secure["spec"]["tls"]["certResolver"] == "letsencrypt"
    middleware_names = {item["name"] for item in secure["spec"]["routes"][0]["middlewares"]}
    assert middleware_names == {"retrievalops-rate-limit", "retrievalops-inflight"}
    assert by_name["retrievalops-rate-limit"]["spec"]["rateLimit"] == {
        "average": 60,
        "period": "1m",
        "burst": 30,
        "sourceCriterion": {"ipStrategy": {"ipv6Subnet": 64}},
    }
    assert by_name["retrievalops-inflight"]["spec"]["inFlightReq"]["amount"] == 32
    assert by_name["retrievalops-https-only"]["spec"]["redirectScheme"] == {
        "scheme": "https",
        "permanent": True,
    }


def test_cleanup_is_hourly_non_concurrent_and_uses_same_immutable_image() -> None:
    cleanup = _document("cleanup.yaml")
    spec = cleanup["spec"]
    container = spec["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]

    assert spec["schedule"] == "17 * * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    assert container["command"] == ["retrievalops-cleanup"]
    assert "@sha256:" in container["image"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    config = _document("config.yaml")["data"]
    assert int(config["RETRIEVALOPS_SANDBOX_TTL_HOURS"]) + 1 <= 24
