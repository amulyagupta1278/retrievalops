# Threat model

## Protected assets

Uploaded content, derived chunks/indexes, capability tokens, judgments, feedback, policy lineage,
database credentials, signing identity, and the production kubeconfig.

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

## Residual risks

The one-node demo is vulnerable to host failure. Rate-limit state is local to the single Traefik
instance and each API replica. Extracted text is not malware-scanned because supported files are
parsed as data and never executed. A stolen live capability token grants access until deletion or
expiry. Production use would require authenticated workspaces, distributed quotas, encrypted
object storage, backups with deletion propagation, secret rotation, and independent penetration
testing.
