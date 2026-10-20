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

    # registration (contract C9; defaults from the M3-06 spike)
    reg_downsample: int = 32  # registration image level (about 7 µm/px)
    reg_coarse_factor: int = 2  # pass 1 runs at reg_downsample * this (64x)
    reg_detector: str = "orb"
    reg_max_features: int = 50_000
    reg_ratio_test: float = 0.8
    # search windows of pass 1 and pass 2; pass 1 was 2500 µm in the spike, narrowed in M3-08 so an
    # unevenly deformed section cannot drift to a precise fit of one region (real pair 4->5)
    reg_window_um: tuple[float, float] = (1000.0, 300.0)
    reg_ransac_px: float = 4.0  # RANSAC tolerance in pixels of the level being fitted
    reg_min_inliers: int = 40
    reg_max_rms_um: float = 25.0
    boundary_min_iou: float = 0.80
    reg_ncc_tolerance: float = 0.01  # feature fit may not correlate clearly worse than the outline

    # tissue mask (contract C4, C9; decision 14: detached fragments are tissue)
    mask_hysteresis: float = 0.75  # pale pixels >= this x threshold count if connected to tissue
    mask_min_component_mm2: float = 0.25  # smaller pieces are debris (specks are below 0.2 mm²)

    @classmethod
    def load(cls, path: Path) -> "ProjectConfig":
        return cls(**yaml.safe_load(path.read_text()))
