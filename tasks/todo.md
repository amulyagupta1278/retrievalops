# RetrievalOps v2 — Implementation Checklist

Every task is a focused S/M increment. The full Definition of Done in `tasks/plan.md` applies to every completed task.

## Day 1 — Secure ingestion foundation

### T01 — Scaffold service and contracts
- [x] Create the locked Python project, configuration, health endpoint, and schemas for sandbox, document, chunk, job, judgment, trace, and policy metadata.
- **Depends on:** none
- **Acceptance:** clean install succeeds; `/healthz` returns service/build identity; schemas reject invalid states.
- **Verify:** `uv sync --frozen && uv run pytest && uv run retrievalops-api-smoke`.

### T02 — Accept protected uploads
- [x] Add multipart PDF/TXT/MD upload with MIME/size/page/text validation, one-time capability token, salted token hash, safe identifiers, and asynchronous job creation.
- **Depends on:** T01
- **Acceptance:** a valid file returns `202`; unsupported, spoofed, oversized, encrypted, scanned, and malformed files fail without retained partial artifacts.
- **Verify:** focused upload/API tests plus a real multipart smoke request.

### T03 — Expire and delete sandboxes
- [x] Add 24-hour expiry, early deletion, idempotent cleanup, and audit evidence without content retention.
- **Depends on:** T02
- **Acceptance:** original and derived artifacts, token hash, judgments, and content-bearing traces are deleted; repeat deletion is safe.
- **Verify:** deletion tests using an injected clock and temporary storage.

### Checkpoint A
- [x] Valid upload creates a protected job; unsafe fixtures fail; early and scheduled deletion are proven.

## Day 2 — First usable retrieval system

### T04 — Extract and chunk documents
- [x] Extract supported formats and create deterministic 512-token chunks with 64-token overlap and stable hashes.
- **Depends on:** T02
- **Acceptance:** repeated extraction is identical; empty/scanned/encrypted inputs fail safely.
- **Verify:** parser and determinism tests for all supported formats.

### T05 — Build three indexes
- [x] Build BM25, dense FAISS, and hybrid RRF indexes asynchronously with recorded model/configuration hashes.
- **Depends on:** T04
- **Acceptance:** all indexes are queryable and isolated by sandbox; partial failure does not mark the job ready.
- **Verify:** focused index tests and clean rebuild comparison.

### T06 — Serve bootstrap hybrid retrieval
- [x] Expose job polling and capability-protected query API using `bootstrap-hybrid`, with ranked passages, latency, trace, policy, and version.
- **Depends on:** T05
- **Acceptance:** query is unavailable before ready and usable after ready; cross-sandbox access fails.
- **Verify:** upload-to-query integration test and runtime smoke.

### Checkpoint B
- [x] A new user uploads a sample document and queries useful passages after ingestion completes.

## Day 3 — Evidence-driven optimization

### T07 — Collect reviewed workload evidence
- [x] Generate five deterministic passage-linked suggestions and accept confirmed/edited judgments.
- **Depends on:** T04, T06
- **Acceptance:** optimization remains locked below three valid judgments; judgments cannot reference another sandbox.
- **Verify:** suggestion determinism and judgment authorization tests.

### T08 — Benchmark retrieval candidates
- [x] Evaluate BM25, dense, and hybrid with Recall@10, nDCG@10, MRR@10, p50/p95 latency, index time, and cost estimate.
- **Depends on:** T05, T07
- **Acceptance:** candidates share one frozen judgment set and produce schema-valid, reproducible scorecards.
- **Verify:** focused metric tests and two-run benchmark comparison.

### T09 — Compile and activate a policy
- [x] Apply hard gates and deterministic tie-breaking, emit an immutable bundle, activate champion, and retain bootstrap fallback.
- **Depends on:** T08
- **Acceptance:** failures include reasons; identical inputs create identical manifests; champion queries expose lineage.
- **Verify:** compiler unit tests and upload-to-selected-policy integration test.

### Checkpoint C
- [x] Upload → ingest → query → review → benchmark → select → query champion works end to end.

## Day 4 — Controlled evidence and lineage

### T10 — Freeze controlled fixtures
- [x] Import and validate the government-schemes and technical-documentation corpora and reviewed evaluations.
- **Depends on:** T04
- **Acceptance:** sources, licenses, revisions, manifests, and hashes are recorded before candidate comparison.
- **Verify:** fixture validation from a clean checkout.

### T11 — Prove cross-corpus reproducibility
- [ ] Run the same benchmark/compiler pipeline for both fixtures and retain every candidate scorecard.
- **Depends on:** T08, T10
- **Acceptance:** repeated runs match within tolerance and honestly report distinct or shared winners.
- **Verify:** clean two-run comparison.

### T12 — Register complete lineage
- [ ] Store fixture and ephemeral policy versions in separate MLflow namespaces with dataset/index/configuration/commit lineage and aliases.
- **Depends on:** T09, T11
- **Acceptance:** missing lineage blocks registration; uploaded text never enters MLflow; a run can be reconstructed from hashes.
- **Verify:** registry tests against local MLflow/PostgreSQL and content-leak assertions.

### Checkpoint D
- [ ] Reconstruct a controlled-fixture decision solely from tracked inputs and lineage.

## Day 5 — Operations and learning

### T13 — Instrument runtime behavior
- [ ] Add structured privacy-safe logs, bounded Prometheus metrics, traces, and alerts for requests, latency, errors, policies, fallback, jobs, drift, and releases.
- **Depends on:** T06, T09
- **Acceptance:** trace IDs connect events; document/query contents are absent by default; alert rules validate.
- **Verify:** observability tests and Prometheus rule validation.

### T14 — Govern feedback and retraining
- [ ] Isolate feedback, add audited approval, detect corpus/query drift, and trigger idempotent benchmark/router retraining.
- **Depends on:** T12, T13
- **Acceptance:** unapproved evidence cannot train; synthetic drift triggers exactly one candidate run; failure preserves champion.
- **Verify:** feedback, drift, idempotency, and failure-path tests.

### Checkpoint E
- [ ] Approved evidence creates a candidate; unapproved evidence cannot affect training or champion.

## Day 6 — Independent release paths

### T15 — Release retrieval policies safely
- [ ] Load candidate/champion concurrently, route deterministic 10/50/100 trace allocation, apply operational gates, and promote or reset candidate traffic.
- **Depends on:** T12–T14
- **Acceptance:** good candidate becomes champion; bad candidate returns to zero traffic without application restart.
- **Verify:** replay promotion and rollback scenarios.

### T16 — Publish application images safely
- [ ] Add reproducible container, lint/type/test/benchmark/security gates, image signing, and immutable GHCR publishing.
- **Depends on:** T13
- **Acceptance:** only protected-main commits passing all gates publish; failures leave production unchanged.
- **Verify:** local container smoke, workflow validation, and green CI run.

### T17 — Roll out application images safely
- [ ] Add K3s/Argo Rollouts readiness, resource limits, Prometheus analysis, promotion, and abort rollback while preserving policy aliases.
- **Depends on:** T16
- **Acceptance:** at least one replica stays ready; good image promotes; bad image aborts; champion never changes.
- **Verify:** good-image and bad-image release smoke scenarios.

### Checkpoint F
- [ ] Good/bad policy releases and good/bad application releases promote or roll back independently with no manual production command.

## Day 7 — Production proof first

### T18 — Secure and publish the demonstration
- [ ] Deploy HTTPS, quotas, scheduled cleanup, security/deletion tests, minimal evidence page, architecture, setup, cost, threat model, runbook, limitations, and demo script.
- **Depends on:** T03, T11, T15, T17
- **Acceptance:** independent reviewer completes the success test; no admin operation or secret is public; evidence types are labeled.
- **Verify:** clean-checkout commands, public API/browser smoke, 24-hour cleanup simulation, and both rollback demonstrations.

### T19 — Optional presentation polish
- [ ] Improve dashboard responsiveness, charts, and advanced analytics only after every mandatory gate passes.
- **Depends on:** T18
- **Acceptance:** polish adds no new backend behavior and cannot delay or weaken mandatory requirements.
- **Verify:** browser, accessibility, and regression tests.

### Final release gate
- [ ] Every mandatory success criterion and Definition of Done item in `tasks/plan.md` has authoritative evidence.
- [ ] Tag `v2.0.0-demo` only after the audit passes; T19 may remain unchecked.
