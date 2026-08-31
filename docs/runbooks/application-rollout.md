# Application canary and rollback

GitHub Actions deploys only an attested, signed image digest after CI gates pass. The production
environment must provide `KUBE_CONFIG_DATA`, `PUBLIC_HOSTNAME`, and a pre-provisioned
`retrievalops-runtime` Secret containing the PostgreSQL, MLflow, dependency-lock, and tracing
settings. Secrets are never rendered into repository manifests.

The API uses the K3s-bundled Traefik v3 traffic router and Argo Rollouts v1.10.0. Traffic moves
through 10%, 50%, and 100%. At every stage, an inline Prometheus analysis requires at least 100
requests, 99% availability, at most 1% errors, and p95 latency no greater than 500 ms. A failed
analysis aborts the Rollout and returns canary traffic to zero. `maxUnavailable: 0`, two API
replicas, readiness probes, and a disruption budget preserve at least one ready API pod.

Only after the API is promoted does the workflow update the queue worker. A failed worker update
is automatically undone. Neither path calls MLflow alias operations, so application rollout and
retrieval-policy champion promotion remain independent.

## Cluster prerequisites

- K3s with Traefik v3 CRDs and Argo Rollouts v1.10.0 installed.
- Prometheus scraping pod annotations and attaching the Kubernetes `pod` target label.
- A PostgreSQL service, MLflow tracking service, OTLP collector, and the runtime Secret.
- GitHub production-environment protection and a narrowly scoped kubeconfig.
- DNS and TLS configured as described by the public-deployment runbook.

Install the pinned Argo Rollouts manifest with server-side apply. The v1.10.0 `Rollout` and
`AnalysisRun` CRDs exceed Kubernetes' client-side-apply annotation limit on a clean cluster:

```sh
kubectl create namespace argo-rollouts
kubectl apply --server-side -n argo-rollouts \
  -f https://github.com/argoproj/argo-rollouts/releases/download/v1.10.0/install.yaml
```

## Evidence for a release

Retain the GitHub run URL, image digest, signature and attestation verification, Rollout status,
AnalysisRun measurements, stable/canary ReplicaSet history, and worker rollout status. A good
image must reach Healthy. A deliberately bad staging image must reach Degraded with canary weight
zero while the previously stable ReplicaSet remains ready. Record the MLflow champion alias before
and after both scenarios; the values must be identical.

The zero digest checked into the manifests is a fail-closed placeholder. The deployment workflow
must replace it with the digest emitted by the publish job; it rejects malformed digests.

The local K3s proof record is retained in `evidence/rollouts/local-k3s-smoke.json`. Its Prometheus
API double only accelerates deterministic good/bad analysis in an ephemeral cluster; production
continues to use the real three-sample Prometheus gates declared in `deploy/k8s/analysis.yaml`.

## Implementation sources

- Argo Rollouts canary specification: https://argo-rollouts.readthedocs.io/en/latest/features/specification/
- Argo Rollouts Traefik v3 integration: https://argo-rollouts.readthedocs.io/en/latest/features/traffic-management/traefik/
- Argo inline Prometheus analysis and automatic abort: https://argo-rollouts.readthedocs.io/en/stable/features/analysis/
- Kubernetes readiness/startup/liveness probes: https://kubernetes.io/docs/concepts/workloads/pods/probes/
- Kubernetes resource requests and limits: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
- Kubernetes disruption budgets: https://kubernetes.io/docs/concepts/workloads/pods/disruptions/
