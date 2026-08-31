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
