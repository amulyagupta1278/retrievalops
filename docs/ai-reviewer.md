# RetrievalOps AI reviewer

The reviewer is a least-privilege GitHub App identity orchestrated by GitHub Actions. After the
`ci` workflow completes for a pull request, a `workflow_run` job checks out only `main`, creates a
short-lived installation token, reads the completed CI run and pull-request diff, asks an AI model
for bounded structured findings, and submits either `APPROVE` or `REQUEST_CHANGES`.

The workflow deliberately does not execute pull-request code with App or model credentials. Text
from the diff and model is untrusted data. The deterministic policy—not the model—controls the
review action.

## Approval gates

Every gate must pass:

1. The PR comes from this repository and its author association is `OWNER`, `MEMBER`, or
   `COLLABORATOR`.
2. The associated `ci` workflow concluded successfully.
3. The `quality` and `container` check runs succeeded on the exact PR head SHA. This includes
   formatting, lint, strict typing, tests, fixture validation, dependency audit, container smoke,
   and Trivy high/critical scanning.
4. The PR does not modify the reviewer's script, workflow, or policy boundary. Those control-plane
   changes require manual administrator review.
5. Every changed file has a reviewable textual patch, the PR is at most 300 files and 80,000 diff
   characters, and added lines do not resemble private keys or common access-token formats.
6. The model call succeeds, returns the exact bounded JSON schema, and reports no high or critical
   findings. Low and medium findings remain visible but do not block approval.

New commits dismiss stale approvals. An unavailable API, malformed model response, missing secret,
oversized/binary diff, or ambiguous workflow-to-PR association fails closed.

## One-time GitHub App setup

Create a private GitHub App named `RetrievalOps AI Reviewer` under **Settings → Developer settings
→ GitHub Apps**. Webhooks are not required because GitHub Actions supplies the event trigger.

Grant only these repository permissions:

- Actions: read
- Checks: read
- Contents: read
- Pull requests: read and write
- Metadata: read (automatically granted)

Install it only on `amulyagupta1278/retrievalops`, generate one private key, and configure:

- Repository variable `AI_REVIEW_APP_ID`: the App ID shown in its settings.
- Repository secret `AI_REVIEW_APP_PRIVATE_KEY`: the complete downloaded PEM contents.
- Repository secret `OPENAI_API_KEY`: a restricted project key with a small monthly budget.
- Repository variable `AI_REVIEW_MODEL`: an available cost-efficient model that supports the
  Responses API.

Delete the downloaded PEM after storing it. Rotate the private key immediately if it is ever
exposed. Do not place any of these values in `.env`, workflow YAML, logs, or pull-request text.

## Safe activation

Keep mandatory reviews disabled while this control-plane PR is introduced. Merge it after manual
review, open a harmless test PR, and verify that the App—not the repository owner—posts an approval
on the current commit. Only then restore branch protection to one required approval with stale
review dismissal enabled. Retain required `quality` and `container` checks, conversation
resolution, force-push/deletion denial, and administrator enforcement.

To disable safely, first set required approvals to zero, then disable this workflow or uninstall
the App. Otherwise every PR will correctly remain blocked.
