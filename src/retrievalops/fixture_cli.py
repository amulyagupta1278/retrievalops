import json
from pathlib import Path

from retrievalops.fixtures import validate_all_fixtures


def main() -> None:
    results = validate_all_fixtures(Path("fixtures"))
    print(json.dumps([result.model_dump() for result in results], indent=2))


if __name__ == "__main__":
    main()
