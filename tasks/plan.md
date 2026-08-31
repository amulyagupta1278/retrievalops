# RetrievalOps v2 — Public Upload Platform Plan

## Outcome

Build a publicly accessible RetrievalOps platform where a user uploads one supported document and, once ingestion completes, receives a usable hybrid retrieval API. The user confirms at least three generated question–passage judgments; RetrievalOps then benchmarks BM25, dense, and hybrid retrieval, selects a policy under explicit quality and latency gates, and safely releases it.

The two fixed corpora remain controlled fixtures for reproducibility, regression tests, drift simulation, canary releases, and rollback proof. The product demonstrates Forward Deployed Engineering, AI engineering, backend engineering, and MLOps through visible operational evidence rather than a RAG answer demo alone.

## Delivery priority

**Functional end-to-end flow beats UI polish.**

If time is constrained, reduce or postpone dashboard complexity, visual polish, advanced analytics, charts, and complicated frontend interactions. Never remove or weaken secure upload and deletion, parsing and indexing, retrieval, reviewed evaluation, policy selection and lineage, observability and drift detection, retraining, canary promotion and rollback, security, reproducibility, or release gates.

## Week-one scope

- One text-based PDF, TXT, or Markdown document per anonymous sandbox, limited to 10 MB and 200 PDF pages.
- Capability-token access, per-IP and per-sandbox rate limits, bounded concurrency, safe filenames, MIME verification, extraction timeouts, restricted CORS, HTTPS, and deletion within 24 hours.
- Asynchronous states: `queued`, `validating`, `extracting`, `indexing`, `ready`, and `failed`.
- Deterministic extraction and 512-token chunks with 64-token overlap.
- BM25, dense FAISS using `all-MiniLM-L6-v2`, and hybrid reciprocal-rank fusion indexes.
- Hybrid retrieval becomes available when ingestion reaches `ready`.
- Five deterministic, passage-linked question suggestions. The user must confirm, edit, or replace at least three question–relevant-passage judgments before optimization.
- Benchmarking with Recall@10, nDCG@10, MRR@10, p50/p95 latency, index time, and estimated serving cost.
- Deterministic policy compilation, immutable bundles, explicit rejection reasons, and safe fallback.
- Separate MLflow namespaces for controlled fixtures and ephemeral user policies; uploaded text is never stored in MLflow.
- Privacy-safe traces, feedback isolation and approval, drift detection, idempotent retraining, policy canary, application canary, automatic promotion, and rollback.
- A minimal public evidence page and API documentation. Dashboard polish is optional after mandatory gates pass.

## Future product scope

- Persistent authenticated workspaces, multi-document corpora, connectors, and incremental ingestion.
- OCR, scanned PDFs, tables, images, archives, and duplicate detection.
- LLM-generated evaluation workloads, expert review, and statistically larger samples.
- Multi-tenancy, billing, regional storage, enterprise access policies, managed vector databases, distributed indexing, and infrastructure high availability.

## Minimum public user flow

1. Upload one supported document.
2. Receive `sandbox_id`, one-time `sandbox_token`, `ingestion_job_id`, and `expires_at`.
3. Poll the job until it becomes `ready` or `failed`.
4. Query the initial `bootstrap-hybrid` policy and receive ranked passages, latency, trace ID, and policy version.
5. Review or edit five proposed workload questions and confirm at least three relevant passages.
6. Start optimization and inspect all three scorecards plus the compiler decision.
7. Query the selected champion policy and inspect its version lineage.
8. Submit isolated feedback, allow automatic deletion after 24 hours, or delete the sandbox early.

## Minimum API

- `POST /v1/sandboxes` — multipart upload; returns sandbox and job metadata.
- `GET /v1/jobs/{job_id}` — returns asynchronous ingestion state and safe failure details.
- `POST /v1/sandboxes/{id}/query` — returns ranked passages, trace, policy, and version.
- `GET /v1/sandboxes/{id}/evaluation-suggestions` — returns proposed questions and source chunks.
- `PUT /v1/sandboxes/{id}/judgments` — confirms or edits question–passage relevance.
- `POST /v1/sandboxes/{id}/optimize` — starts benchmark and compilation after the evidence minimum is met.
- `GET /v1/sandboxes/{id}/policy` — returns benchmark evidence, compiler decision, and active version.
- `POST /v1/sandboxes/{id}/feedback` — stores unapproved feedback separately.
- `DELETE /v1/sandboxes/{id}` — performs early deletion.

All sandbox endpoints require `X-Sandbox-Token`; only a salted token hash is persisted. Administrative approval and release operations are never exposed publicly.

## Architecture

```text
Upload -> validate -> extract -> chunk -> BM25/dense/hybrid indexes
                                      |             |
                                      |       bootstrap-hybrid API
                                      v             |
                            reviewed judgments <----+
                                      |
                            benchmark + compiler
                                      |
                         immutable MLflow policy bundle
                                      |
                       candidate/champion trace router

Approved feedback -> drift/retrain -----------^

Code commit -> GitHub Actions -> GHCR -> Argo application canary
                                             |
                                      Prometheus health gates
```

- PostgreSQL stores sandbox metadata, job state, token hashes, approvals, and audit records.
- Uploaded and derived artifacts live in sandbox-specific storage carrying an expiry marker.
- MLflow stores hashes, metrics, aliases, and lineage but not uploaded content.
- A stable API loads `champion` and `candidate`; a hash of corpus ID and trace ID produces deterministic 10/50/100 policy allocation.
- Policy rollback sets candidate allocation to zero without restarting the application.
- Application images follow a separate GitHub Actions/GHCR/K3s/Argo Rollouts path. Application rollback never changes the champion policy.
- One small K3s VPS is production-shaped, not infrastructure-highly-available. Zero downtime means at least one ready application replica throughout rollout.

## Release contract

A policy candidate cannot receive live or replay traffic unless schema, unit, integration, security, deletion, and reproducibility tests pass; no must-pass judgment regresses; Recall@10 and nDCG@10 decline by no more than 0.02 absolute versus champion; reference p95 retrieval latency is at most 500 ms; and the bundle records commit, configuration, dependency, dataset, index, and policy hashes.

Among passing candidates, select highest nDCG@10, then Recall@10, then lower p95 latency, then lower estimated cost. Ties are deterministic. Offline reviewed evidence establishes quality. Online policy canaries measure availability, error rate, latency, load failures, and fallback rate. Approved delayed feedback informs later releases; replay traffic is visibly labeled synthetic.

## Seven-day implementation order

| Day | Tasks | Mandatory checkpoint |
|---|---|---|
| 1 | T01–T03: scaffold, contracts, secure upload, expiry/deletion | Valid upload creates a job; unsafe input fails; deletion is proven |
| 2 | T04–T06: extraction, chunks, indexes, hybrid query | Upload-to-query works after asynchronous ingestion |
| 3 | T07–T09: reviewed evidence, benchmark, compiler | Upload-to-selected-policy works end to end |
| 4 | T10–T12: fixed fixtures, reproducibility, MLflow lineage | A decision reconstructs from tracked hashes |
| 5 | T13–T14: observability, feedback, drift, retraining | Approved evidence creates a candidate; unapproved evidence cannot |
| 6 | T15–T17: policy release and application release paths | Good and bad releases promote/rollback independently |
| 7 | T18: HTTPS, cleanup, security, runbooks; T19 optional UI polish | Independent reviewer completes success test |

## Success test and Definition of Done

The release is complete only when:

- Supported uploads progress asynchronously to `ready`; invalid, oversized, encrypted, scanned, and malformed documents fail safely.
- Hybrid retrieval works after ingestion and before optimization.
- Optimization cannot start without at least three human-confirmed judgments.
- All three scorecards contain the declared retrieval and operational metrics, and the compiler records why candidates passed, failed, or won.
- Queries return ranked passages, latency, trace ID, policy, and version.
- Controlled-fixture runs reproduce within declared tolerance; lineage identifies every required hash.
- Unapproved feedback cannot enter evaluation or training; drift starts exactly one idempotent candidate workflow.
- Good policies promote without an application restart; bad policies return to zero traffic automatically.
- Good application images promote without downtime; bad images abort without changing `champion`.
- Logs exclude document and query contents by default; metrics use bounded-cardinality labels.
- Uploaded content, indexes, judgments, content-bearing traces, and credentials are deleted within 24 hours.
- Unit, integration, security, deletion, reproducibility, API, container, and browser tests pass.
- The public deployment uses HTTPS, exposes no administrative operation, and clearly labels controlled, reviewed, synthetic, and replay evidence.
- Architecture, setup, cost, threat model, runbook, limitations, and five-minute demonstration are documented.

Dashboard polish is not part of completion when all mandatory functional and production gates pass.
