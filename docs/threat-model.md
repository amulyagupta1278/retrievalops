# Threat model

## Protected assets

Uploaded content, derived chunks/indexes, capability tokens, judgments, feedback, policy lineage,
database credentials, signing identity, and the production kubeconfig.
The AI reviewer additionally protects its GitHub App private key, installation tokens, model API
key, review policy, and the integrity of merge approvals.

## Trust boundaries and controls

- Internet traffic terminates at Traefik HTTPS. Per-IP token-bucket and in-flight limits bound
  abuse before requests reach FastAPI; per-sandbox limits prevent one capability from monopolizing
  a replica.
- Uploads allow only PDF/TXT/Markdown, enforce extension/MIME agreement, size/page/text checks,
  safe basenames, strict PDF parsing, and fail-closed cleanup of partial artifacts.
- Capability tokens are returned once and persisted only as salted hashes. Cross-sandbox failures
  are returned as 404 to reduce identifier disclosure. Administrative approval/release operations
  do not exist in the public OpenAPI surface.
- Logs and metrics use route templates and bounded labels; document/query contents and tokens are
  excluded. MLflow registrations reject missing lineage and never store uploaded text.
- Unapproved feedback cannot enter training. Candidates cannot serve until offline gates pass;
  online policy and application regressions automatically return to zero traffic.
- Containers run as UID 10001, drop Linux capabilities, prohibit privilege escalation, use a
  read-only root filesystem, bounded resources, and a default-deny network policy.
- Hourly cleanup deletes expired originals, indexes, judgments, feedback, approvals, and token
  hashes. Early deletion is idempotent and produces only non-content audit evidence.
- AI review runs only after CI from a workflow stored on `main`. It checks out `main`, never PR
  code, while holding its short-lived App token. Forks and untrusted authors are rejected;
  reviewer-control changes require manual review. Diff and model text are untrusted data, model
  output is schema-validated and bounded, detected credentials are not sent to the model, and any
  missing or ambiguous result fails closed.

## Residual risks

The one-node demo is vulnerable to host failure. Rate-limit state is local to the single Traefik
instance and each API replica. Extracted text is not malware-scanned because supported files are
parsed as data and never executed. A stolen live capability token grants access until deletion or
expiry. Production use would require authenticated workspaces, distributed quotas, encrypted
object storage, backups with deletion propagation, secret rotation, and independent penetration
testing.
AI review can still miss semantic defects or produce false positives. Deterministic CI/security
gates remain authoritative, low/medium model findings are advisory, and high/critical findings
block until the App re-reviews a newer commit. Pull-request diffs are disclosed to the configured
model provider, so private or regulated repositories need an approved provider and retention
policy before enabling this design.
