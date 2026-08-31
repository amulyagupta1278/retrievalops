# Public deployment runbook

## Prerequisites

Provision one x86 K3s node with DNS pointing to it. Install Argo Rollouts v1.10.0, Prometheus,
PostgreSQL, MLflow, and an OTLP collector. Configure K3s Traefik v3 with an ACME resolver named
`letsencrypt`; the checked IngressRoute requests certificates through that resolver. Restrict the
host firewall to SSH, HTTP, and HTTPS and limit SSH by source IP where possible.

Create `retrievalops-runtime` in the `retrievalops` namespace with:

- `RETRIEVALOPS_DATABASE_URL` (PostgreSQL, TLS enabled)
- `RETRIEVALOPS_MLFLOW_TRACKING_URI`
- `RETRIEVALOPS_DEPENDENCY_LOCK_HASH` (64 lowercase hex characters)
- `RETRIEVALOPS_OTLP_TRACES_ENDPOINT`

Never commit the Secret. Create a narrowly scoped base64 kubeconfig as GitHub environment secret
`KUBE_CONFIG_DATA`; set repository variable `PUBLIC_HOSTNAME`. Protect the `production` environment
and configure the `main` ruleset in `docs/runbooks/ci-release.md`.

## Release

A merged commit runs every gate, publishes and verifies one immutable digest, then renders the
hostname/digest into the manifests. Argo routes 10%, 50%, and 100% only after Prometheus analysis.
The worker and cleanup CronJob update after API promotion. There is no manual production command.

After release, verify HTTPS certificate/redirect, `/healthz`, `/`, `/docs`, upload-to-query,
three-reviewed-judgment optimization, Prometheus targets, traces, hourly cleanup, and both release
planes. Retain evidence named in `docs/runbooks/application-rollout.md`.

Production sandboxes expire after 23 hours. The hourly cleanup schedule provides a one-hour safety
margin so deletion completes before the public 24-hour promise even at the worst schedule offset.

## Rollback and incident response

Argo automatically aborts a failed API canary; the worker workflow automatically runs Deployment
undo. For security compromise, revoke the GitHub environment secret, rotate runtime credentials,
set public DNS to maintenance, delete affected sandboxes, retain content-free audit records, and
redeploy the last verified digest. Policy aliases are investigated separately and must never be
changed as a side effect of application rollback.

Traefik implementation references:
https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/ratelimit/,
https://doc.traefik.io/traefik/master/reference/routing-configuration/http/middlewares/redirectscheme/,
and https://doc.traefik.io/traefik/reference/routing-configuration/http/tls/overview/.
