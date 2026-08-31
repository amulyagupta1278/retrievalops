# Controlled retrieval fixtures

These fixtures are benchmark and release-control evidence, not anonymous user uploads.
Their manifests freeze source revision, attribution, evidence status, record counts, and
SHA-256 hashes. `government-schemes` uses the dissertation's valid seed-42
owner-adjudicated qrels; the invalid seed-123 predecessor is deliberately excluded.
`technical-documentation` uses SciFact's expert-annotated scientific retrieval test set.

Run `uv run retrievalops-validate-fixtures` before any benchmark or release workflow.
