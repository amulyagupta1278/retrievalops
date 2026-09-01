from __future__ import annotations

import importlib.util
from pathlib import Path


def load_reviewer():
    path = Path(__file__).parents[1] / "scripts" / "ai_reviewer.py"
    spec = importlib.util.spec_from_file_location("ai_reviewer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gate_approves_only_trusted_pr_with_successful_ci_and_clean_findings() -> None:
    reviewer = load_reviewer()

    decision = reviewer.decide(
        trusted_author=True,
        same_repository=True,
        ci_conclusion="success",
        deterministic_findings=[],
        ai_findings=[],
    )

    assert decision.approved is True
    assert decision.reasons == ()


def test_gate_fails_closed_when_ai_review_is_unavailable() -> None:
    reviewer = load_reviewer()

    decision = reviewer.decide(
        trusted_author=True,
        same_repository=True,
        ci_conclusion="success",
        deterministic_findings=[],
        ai_findings=None,
    )

    assert decision.approved is False
    assert "AI review did not return a valid decision" in decision.reasons


def test_gate_blocks_high_severity_ai_finding() -> None:
    reviewer = load_reviewer()
    finding = reviewer.Finding(
        "ai-review", "high", "src/app.py", 4, "Unsafe command", "Input reaches a shell."
    )

    decision = reviewer.decide(
        trusted_author=True,
        same_repository=True,
        ci_conclusion="success",
        deterministic_findings=[],
        ai_findings=[finding],
    )

    assert decision.approved is False
    assert "AI review found high or critical issues" in decision.reasons


def test_gate_rejects_untrusted_fork_even_when_all_checks_pass() -> None:
    reviewer = load_reviewer()

    decision = reviewer.decide(
        trusted_author=False,
        same_repository=False,
        ci_conclusion="success",
        deterministic_findings=[],
        ai_findings=[],
    )

    assert decision.approved is False
    assert "pull request is not from this repository" in decision.reasons
    assert "author association is not trusted" in decision.reasons


def test_scan_patch_blocks_reviewer_tampering_and_detects_private_keys() -> None:
    reviewer = load_reviewer()
    files = [
        {
            "filename": "scripts/ai_reviewer.py",
            "status": "modified",
            "patch": "@@ -1 +1 @@\n-old\n+disabled = True",
        },
        {
            "filename": "config.py",
            "status": "modified",
            "patch": "@@ -1 +1 @@\n+KEY = '-----BEGIN PRIVATE KEY-----'",
        },
    ]

    findings = reviewer.scan_files(files)

    assert any(item.rule == "reviewer-integrity" for item in findings)
    assert any(item.rule == "secret-material" for item in findings)


def test_scan_patch_requires_manual_review_when_github_omits_patch() -> None:
    reviewer = load_reviewer()

    findings = reviewer.scan_files([{"filename": "large.bin", "status": "modified"}])

    assert [item.rule for item in findings] == ["unreviewable-diff"]


def test_parse_ai_findings_accepts_only_bounded_structured_output() -> None:
    reviewer = load_reviewer()

    findings = reviewer.parse_ai_findings(
        '{"findings":[{"severity":"high","path":"src/app.py",'
        '"line":12,"title":"Command injection","body":"User input reaches a shell."}]}'
    )

    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].path == "src/app.py"


def test_parse_ai_findings_rejects_unknown_shape() -> None:
    reviewer = load_reviewer()

    assert reviewer.parse_ai_findings('{"approved":true}') is None


def test_workflow_never_checks_out_pull_request_code_with_app_credentials() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "ai-review.yml").read_text()

    assert "workflow_run:" in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "permission-pull-requests: write" in workflow
    assert "pull_request_target" not in workflow
