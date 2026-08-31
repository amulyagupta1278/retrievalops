import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import UUID


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write_source(self, sandbox_id: UUID, content: bytes) -> str:
        sandbox_directory = self.root / str(sandbox_id)
        sandbox_directory.mkdir(parents=True, exist_ok=False)
        destination = sandbox_directory / "source"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=sandbox_directory, delete=False) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            temporary_path.replace(destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return f"{sandbox_id}/source"

    def delete_sandbox(self, sandbox_id: UUID) -> None:
        directory = self.root / str(sandbox_id)
        if directory.exists():
            shutil.rmtree(directory)

    def read(self, storage_key: str) -> bytes:
        path = self._resolve(storage_key)
        return path.read_bytes()

    def write_bytes(self, sandbox_id: UUID, name: str, content: bytes) -> str:
        if Path(name).name != name:
            raise ValueError("artifact name must not contain path components")
        destination = self.root / str(sandbox_id) / name
        self._atomic_write(destination, content)
        return f"{sandbox_id}/{name}"

    def write_json(self, sandbox_id: UUID, name: str, value: object) -> str:
        content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.write_bytes(sandbox_id, name, content)

    def sandbox_path(self, sandbox_id: UUID, name: str) -> Path:
        return self._resolve(f"{sandbox_id}/{name}")

    def _resolve(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        root = self.root.resolve()
        if root not in candidate.parents:
            raise ValueError("artifact key escapes storage root")
        return candidate

    @staticmethod
    def _atomic_write(destination: Path, content: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            temporary_path.replace(destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
