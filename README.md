# RetrievalOps

RetrievalOps turns one uploaded PDF, TXT, or Markdown document into a protected retrieval API,
then uses at least three human-reviewed question–passage judgments to benchmark BM25, dense, and
hybrid retrieval and activate the best policy that passes frozen quality and latency gates.

This repository is a production-shaped public demonstration of AI engineering, backend
engineering, and MLOps maturity: content-safe lineage, drift-triggered retraining, policy canaries,
application canaries, observability, automatic rollback, and deletion within 24 hours.

## Run locally

Requirements: Python 3.12.14 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen
uv run retrievalops-api
```

In another terminal:

```sh
uv run retrievalops-worker
```

Open `http://127.0.0.1:8000/` for evidence and `http://127.0.0.1:8000/docs` for the live API.
The default local database is SQLite; production requires PostgreSQL and a shared artifact volume.

## Prove the repository

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run retrievalops-validate-fixtures
uv run retrievalops-benchmark-fixtures --output /tmp/retrievalops-evidence
uvx --from pip-audit==2.10.1 pip-audit --path .venv/lib/python3.12/site-packages --skip-editable
kubectl kustomize deploy/k8s
```

The hosted workflow additionally runs PostgreSQL lineage integration, Prometheus validation,
container smoke/security scanning, immutable GHCR publication, Sigstore signing and attestation,
and an Argo Rollouts 10/50/100 application canary.

## API flow

1. `POST /v1/sandboxes` with one multipart document; save the returned capability token.
2. Poll `GET /v1/jobs/{job_id}` with `X-Sandbox-Token` until `ready`.
3. Query `POST /v1/sandboxes/{id}/query`; bootstrap hybrid works before optimization.
4. Get five suggestions and submit at least three reviewed judgments.
5. Call `POST /v1/sandboxes/{id}/optimize`, then inspect `/policy` and query the champion.
6. Delete early with `DELETE /v1/sandboxes/{id}` or allow hourly cleanup after 24 hours.

See [architecture](docs/architecture.md), [threat model](docs/threat-model.md),
[deployment](docs/public-deployment.md), [cost](docs/cost.md), [limitations](docs/limitations.md),
and the [five-minute demo](docs/demo.md).
