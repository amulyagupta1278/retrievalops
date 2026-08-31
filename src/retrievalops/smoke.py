from retrievalops.api import health


def main() -> None:
    response = health()
    if response.status != "ok":
        raise SystemExit(1)
    print(response.model_dump_json())
