"""Minimal Prometheus HTTP API double for local Argo Rollouts smoke tests."""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        query = parse_qs(urlparse(self.path).query).get("query", [""])[0]
        mode = os.environ.get("PROMETHEUS_MODE", "good")
        value = _result(query, mode)
        body = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [time.time(), str(value)]}],
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _result(query: str, mode: str) -> float:
    if "histogram_quantile" in query:
        return 0.9 if mode == "bad" else 0.1
    if 'status_class="5xx"' in query:
        return 0.2 if mode == "bad" else 0.0
    if 'status_class!="5xx"' in query:
        return 0.8 if mode == "bad" else 1.0
    return 100.0


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9090), Handler).serve_forever()
