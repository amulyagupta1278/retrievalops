# RetrievalOps observability runbook

Telemetry is intentionally content-free: do not add document text, query text, capability
tokens, sandbox IDs, or request IDs to metric labels. Correlate an individual request with
the `X-Request-ID` and `X-Trace-ID` response headers.

## High error rate

Group `retrievalops_http_requests_total` by route and status class, locate a failing trace,
and inspect its content-free structured event. Stop a live candidate or application rollout
if the increase aligns with that release.

## High query latency

Compare the query latency histogram by policy and inspect the slow trace. Reset candidate
traffic to zero if only candidate traffic breaches the 500 ms p95 release gate.

## Ingestion failures

Check transition counts and correlated `ingestion_job_failed` events. Confirm storage,
parsing, and embedding health without inspecting uploaded content. Preserve failed state and
its bounded error code for the user.

## Fallback spike

Group fallback events by the bounded reason label. Confirm the champion bundle and indexes
load successfully; keep bootstrap fallback active and stop candidate traffic if implicated.

## Drift workflow failure

Confirm the idempotency key, approved evidence version, and candidate workflow status. The
champion must remain unchanged; retry only after the bounded failure reason is understood.

## Release rollback

Group the release counter by `kind` and `outcome`, inspect the correlated rollout trace,
and verify traffic returned to the champion policy or prior application ReplicaSet. Escalate
if rollback itself did not restore the user-facing error and latency gates.
