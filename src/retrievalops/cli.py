import uvicorn


def main() -> None:
    uvicorn.run("retrievalops.api:app", host="0.0.0.0", port=8000)
