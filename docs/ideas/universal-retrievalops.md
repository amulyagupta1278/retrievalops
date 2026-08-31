# Universal RetrievalOps

## Problem Statement

How might we help a small AI team continuously discover and safely operate the best retrieval policy for its corpus and real query distribution, without requiring a dedicated retrieval-evaluation or MLOps team?

## Recommended Direction

Build RetrievalOps as a closed-loop release controller for retrieval policies. Given a corpus and a reviewed evaluation set, it benchmarks candidate retrieval configurations, selects a quality-latency-cost policy, packages that policy as a versioned release, and serves it through a stable live API.

After deployment, RetrievalOps records queries, evidence, policy versions, runtime metrics, and explicit feedback. Corpus changes or query-distribution drift trigger a new evaluation and router-training run. A candidate policy receives canary traffic and is promoted or rolled back automatically according to frozen quality and reliability gates.

The adaptive router is a component, not the product. The differentiator is the full lifecycle from corpus-specific discovery through observable, reversible production deployment.

## Key Assumptions to Validate

- [ ] Corpus-specific policy selection beats a fixed hybrid baseline on quality-adjusted cost across two structurally different corpora. Test with the dissertation corpus and one independent corpus using the same frozen protocol.
- [ ] A small reviewed benchmark is sufficient to select a useful initial policy. Test stability when using 10, 20, and the complete available question set.
- [ ] Live failures can be converted into regression cases without silently poisoning the benchmark. Require explicit approval before feedback becomes evaluation ground truth.
- [ ] Canary metrics can distinguish a harmful policy from ordinary query variance. Demonstrate promotion of one candidate and automatic rollback of another under controlled traffic.
- [ ] A stable adapter contract can support additional retrievers and storage systems without changing the policy lifecycle.

## MVP Scope

- One public web application and stable retrieval API.
- Two corpora: the government-scheme dissertation corpus and one different public technical-document corpus.
- Corpus ingestion for text, Markdown, HTML, and text-based PDF documents.
- Three production candidate retrievers: BM25, dense, and hybrid RRF.
- Existing graph and LLM-reranker dissertation results available as comparison evidence, not required production dependencies.
- Reviewed question-and-relevance dataset required for trustworthy policy selection; synthetic questions may be proposed but cannot become canonical without approval.
- Automated benchmark using retrieval quality, evidence coverage, p95 latency, and estimated cost.
- Versioned policy bundle containing retriever choice, parameters, index/corpus identity, router version, thresholds, and provenance.
- Live query routing with immutable prediction traces and explicit user feedback.
- Drift trigger that launches re-evaluation and router retraining.
- CI quality gates, model/policy registry, canary release, automated promotion, and rollback.
- Public demonstration showing two corpora selecting different policies and a deliberately bad candidate being rolled back.

## Not Doing (and Why)

- Every vector database or retrieval framework - adapters come after the lifecycle works end to end.
- Multimodal retrieval - UniversalRAG already targets modality breadth; it does not test this project's differentiator.
- Unreviewed fully synthetic ground truth - corpus inspection cannot reveal actual user intent, and false labels would invalidate policy selection.
- Reinforcement learning or online bandits - insufficient safe traffic and unnecessary for the first proof.
- Fully autonomous promotion without frozen gates - zero-touch execution is acceptable only inside explicit quality, reliability, and rollback constraints.
- A general chatbot product - answering questions is the demonstration surface, not the core user value.
- Enterprise authentication, billing, or multi-region infrastructure - they do not test the principal product assumption this week.

## Open Questions

- Which independent public corpus should serve as the second domain?
- What exact utility function should combine retrieval quality, latency, and cost without hiding regressions behind one composite score?
- Which low-cost hosting target can support a real canary demonstration within the one-week constraint?
- Should the first router select one corpus-wide policy, a per-query policy, or use corpus-wide defaults with confidence-based escalation?
