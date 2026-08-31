# Limitations

- One text-based PDF, TXT, or Markdown document per anonymous 24-hour sandbox.
- No OCR, scanned PDFs, images, tables, archives, HTML, connectors, or incremental ingestion.
- Five deterministic suggestions and three confirmations demonstrate the workflow; they are not a
  statistically strong evaluation for a business-critical corpus.
- Dense retrieval uses one frozen MiniLM revision and CPU FAISS. No reranker or generative answer
  layer is included.
- The public demo is a single K3s node with shared storage. Application rollout is zero-downtime;
  infrastructure is not highly available.
- Per-IP and per-sandbox quotas are in-memory in this one-node design. Multi-node production needs
  a shared rate-limit backend.
- The controlled fixtures prove reproducibility and release machinery, not universal superiority
  of hybrid retrieval. Both current fixtures select hybrid because must-pass gates participate in
  compilation, not merely aggregate metric ranking.
