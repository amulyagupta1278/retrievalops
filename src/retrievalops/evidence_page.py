from fastapi.responses import HTMLResponse


def evidence_page() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RetrievalOps — evidence-driven retrieval</title>
  <link rel="icon" href="data:,">
</head>
<body>
  <header>
    <h1>RetrievalOps</h1>
    <p>Upload a supported document, query it after ingestion, review evidence, and release the
    retrieval policy that passes explicit quality and latency gates.</p>
    <p><a href="/docs">Open the live API</a> · <a href="/healthz">Service health</a></p>
  </header>
  <main>
    <section aria-labelledby="flow">
      <h2 id="flow">Public flow</h2>
      <ol>
        <li>Upload one text-based PDF, TXT, or Markdown document.</li>
        <li>Poll the asynchronous job until it is ready.</li>
        <li>Query the bootstrap hybrid index.</li>
        <li>Confirm at least three question–passage judgments.</li>
        <li>Benchmark BM25, dense, and hybrid; activate only a passing policy.</li>
      </ol>
      <p>Sandboxes are capability-protected and deleted within 24 hours.</p>
    </section>
    <section aria-labelledby="evidence">
      <h2 id="evidence">Controlled evidence</h2>
      <p><strong>Controlled, human-reviewed evidence.</strong> Both frozen fixtures reproduce
      exact quality metrics and index/configuration hashes within declared timing tolerances.</p>
      <table>
        <caption>Latest checked benchmark decisions</caption>
        <thead><tr><th>Corpus</th><th>Selected policy</th><th>Recall@10</th>
        <th>nDCG@10</th><th>p95</th></tr></thead>
        <tbody>
          <tr><td>Government schemes</td><td>Hybrid</td><td>0.7453</td>
          <td>0.7833</td><td>8.72 ms</td></tr>
          <tr><td>Technical documentation</td><td>Hybrid</td><td>0.8179</td>
          <td>0.6860</td><td>74.25 ms</td></tr>
        </tbody>
      </table>
      <p>Selection also enforces must-pass judgments; the highest aggregate metric is not allowed
      to override a failed hard gate.</p>
    </section>
    <section aria-labelledby="release">
      <h2 id="release">Release controls</h2>
      <ul>
        <li><strong>Reviewed:</strong> user judgments remain locked until three are confirmed.</li>
        <li><strong>Synthetic/replay:</strong> used only for controlled release verification.</li>
        <li><strong>Live:</strong> policy and application canaries promote or roll back
        independently.</li>
      </ul>
      <p>Images are immutable, signed, attested, and released through 10/50/100 traffic gates.
      Uploaded text and query text are excluded from logs and MLflow.</p>
    </section>
    <section aria-labelledby="limits">
      <h2 id="limits">Demo limits</h2>
      <p>One document per anonymous sandbox; no OCR, scanned PDFs, tables, archives, connectors,
      billing, or infrastructure high availability. This is a production-shaped public demo.</p>
    </section>
  </main>
</body>
</html>"""
    )
