"""File layout of the work directory. The only place that knows these names."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkDir:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def volume(self) -> Path:
        return self.root / "volume.zarr"
