from datetime import UTC, datetime
from uuid import UUID

from retrievalops.metadata import MetadataStore
from retrievalops.storage import ArtifactStore


class SandboxLifecycle:
    def __init__(self, metadata_store: MetadataStore, artifact_store: ArtifactStore) -> None:
        self._metadata_store = metadata_store
        self._artifact_store = artifact_store

    def delete(self, sandbox_id: UUID, *, now: datetime, reason: str) -> bool:
        self._artifact_store.delete_sandbox(sandbox_id)
        return self._metadata_store.delete_sandbox(
            sandbox_id,
            deleted_at=now,
            reason=reason,
        )

    def cleanup_expired(self, now: datetime | None = None) -> int:
        cleanup_time = now or datetime.now(UTC)
        identifiers = self._metadata_store.expired_sandbox_ids(cleanup_time)
        for sandbox_id in identifiers:
            self.delete(sandbox_id, now=cleanup_time, reason="TTL_EXPIRED")
        return len(identifiers)
