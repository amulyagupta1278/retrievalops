from fastapi import Response
from fastapi.responses import HTMLResponse


def api_docs_page() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RetrievalOps - Swagger UI</title>
  <link rel="icon" href="data:,">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script src="/docs-init.js"></script>
</body>
</html>"""
    )


def api_docs_script() -> Response:
    return Response(
        """SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  deepLinking: true,
  displayRequestDuration: true,
  persistAuthorization: false
});
""",
        media_type="application/javascript",
    )
