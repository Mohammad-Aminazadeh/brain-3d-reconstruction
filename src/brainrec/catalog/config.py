"""Project configuration (YAML)."""

from pathlib import Path

import yaml
from pydantic import BaseModel


class ProjectConfig(BaseModel):
    data_dir: Path
    work_dir: Path
    z_spacing_um: float  # distance between sections; PLACEHOLDER until the lab supplies it
    base_downsample: int = 4  # volume base level: 4 = 0.88 um/px
    n_levels: int = 5  # pyramid levels: base, 2x, 4x ... coarser

    @classmethod
    def load(cls, path: Path) -> "ProjectConfig":
        return cls(**yaml.safe_load(path.read_text()))
