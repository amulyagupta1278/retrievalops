# Five-minute demonstration

## Before the interview

Open the evidence page, API docs, GitHub Actions run, MLflow lineage, Prometheus dashboard, and
Argo Rollout status. Prepare one short text file, one successful policy canary record, and one
failed application canary record. Never expose tokens, kubeconfig, database URLs, or user content.

## Script

**0:00–0:40 — Product.** Show the public evidence page. Explain that RetrievalOps creates a usable
retrieval system first, then optimizes only after human evidence—not an LLM answer demo.

**0:40–1:40 — Ingestion.** Upload the sample in Swagger, save the one-time token, and poll the job.
Call out asynchronous states and the separate durable worker. Query `bootstrap-hybrid` when ready.

**1:40–2:40 — Evaluation.** Show five passage-linked suggestions. Confirm three. Run optimize and
compare BM25, dense, and hybrid scorecards, hard-gate reasons, latency, and the deterministic winner.

**2:40–3:30 — Lineage.** Query the champion and show policy/version/trace. In MLflow, show commit,
dependency, corpus, configuration, index, evidence, and policy hashes—never document text.

**3:30–4:20 — Operations.** Show privacy-safe logs, bounded Prometheus metrics, drift triggering one
idempotent retraining candidate, and a bad policy returning to zero traffic without an app restart.

**4:20–5:00 — Delivery and privacy.** Show the signed image digest, application AnalysisRun,
automatic bad-image abort, unchanged MLflow champion, and early deletion. End with honest one-node
and small-evaluation limitations.
