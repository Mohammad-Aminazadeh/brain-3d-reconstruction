"""Project configuration (YAML)."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a misspelled setting is an error, not a default

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
    # weight of the tissue edge relative to the centre (1) in fits and correlations: the centre
    # must align best, the edge still counts (owner, 2026-11-04; decision 0004). Proposed value.
    reg_edge_weight: float = 0.2
    reg_retry_window_um: float = 2500.0  # pass-1 window of the second attempt of a failing pair

    # tissue mask (contract C4, C9; decision 14: detached fragments are tissue)
    mask_hysteresis: float = 0.75  # pale pixels >= this x threshold count if connected to tissue
    mask_min_component_mm2: float = 0.25  # smaller pieces are debris (specks are below 0.2 mm²)

    # registered volume (contract C9, C11; decision 9: a rim of up to about 50 µm is accepted)
    volume_mask_margin_um: float = 50.0

    # quality control (contract C9, C10): a pair is flagged when a score is below its threshold;
    # None = not checked. Calibrated on real slices 1 to 10 in M4-05 (approved for now,
    # 2026-11-07): both lie in the gap between the damaged pairs 4->5, 5->6 and the rest. Together they catch a 5° rotation
    # error in every pair, a 400 µm shift only in the weaker pairs; errors of 100 to 200 µm change
    # the scores less than the scores differ between pairs.
    qc_min_ncc: float | None = 0.40  # real pairs 0.346 to 0.754
    qc_min_dice: float | None = 0.93  # real pairs 0.904 to 0.981; reflects damaged margins

    # nucleus detection (contract C9, C13; decided after the M7-01 spike)
    detect_downsample: int = 2  # detection level: 2x = 0.44 µm/px
    detect_method: str = "instanseg"  # or "stardist" (second candidate, M7-10)
    detect_tile_px: int = 1024  # read window per tile at the detection level
    detect_overlap_um: float = 40.0  # windows overlap; a nucleus belongs to the tile of its centre
    detect_min_area_um2: float = 8.0  # smaller profiles are dropped
    detect_patch_min_diameter_um: float = 8.0  # from this size a 1x patch gives size and nucleolus
    detect_min_sharpness: float = 100.0  # tiles below are blurred (level-0 scale of M7-01)

    @classmethod
    def load(cls, path: Path) -> "ProjectConfig":
        """Settings from a YAML file. Raises pydantic.ValidationError on missing, unknown or
        invalid settings, and TypeError if the file holds no `name: value` settings at all."""
        values = yaml.safe_load(path.read_text())
        if not isinstance(values, dict):
            raise TypeError(f"{path} holds no settings (expected `name: value` lines)")
        return cls.model_validate(values)
