# Architecture

```text
Traefik HTTPS + IP/concurrency quotas
                 |
        Argo API stable/canary
          |             |
      PostgreSQL    shared artifacts
          |             |
       queue <------ upload
          |
       worker -> extract -> chunks -> BM25 + MiniLM/FAISS + hybrid RRF
                                      |
reviewed judgments -> benchmark -> compiler -> immutable MLflow lineage
                                      |
approved feedback -> drift -> retrain -> policy 10/50/100 canary

commit -> CI gates -> signed/attested GHCR digest -> application 10/50/100 canary
```

The application and policy release planes are independent. An application rollback does not
change MLflow `champion`; a policy rollback sets candidate allocation to zero without restarting
the API. PostgreSQL coordinates durable jobs and approval state. Uploaded and derived content is
kept only in the sandbox artifact namespace; MLflow receives hashes, metrics, aliases, and lineage.

The public demo runs on one K3s node. Two API replicas plus `maxUnavailable: 0`, readiness probes,
Traefik weighted routing, and a disruption budget provide zero-downtime application rollout, but
the cluster itself is not highly available.
