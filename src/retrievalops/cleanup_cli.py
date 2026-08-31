import json

from retrievalops.config import get_settings
from retrievalops.lifecycle import SandboxLifecycle
from retrievalops.metadata import MetadataStore
from retrievalops.storage import ArtifactStore


def main() -> None:
    settings = get_settings()
    metadata = MetadataStore(settings.database_url)
    metadata.initialize()
    lifecycle = SandboxLifecycle(metadata, ArtifactStore(settings.storage_root))
    print(json.dumps({"deleted_sandboxes": lifecycle.cleanup_expired()}))


if __name__ == "__main__":
    main()
