"""Local storage layout. Each developer keeps their own; never commit or share it."""

from dataclasses import dataclass
from pathlib import Path

from .migrations import migrate
from .sqlite import enable_wal


@dataclass(frozen=True)
class StoragePaths:
    """Separate files so graph checkpoints never mix with application records."""

    root: Path

    @property
    def app_db(self) -> Path:
        return self.root / "ghostlab.sqlite"

    @property
    def graph_db(self) -> Path:
        return self.root / "graph-checkpoints.sqlite"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"


def open_database(path: Path) -> Path:
    """Create or upgrade the application database; returns its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    enable_wal(path)
    migrate(path)
    return path
