"""Project configuration (YAML)."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CellType(BaseModel):
    """One row of the type table (contract C14). Ids are stable and never reused."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)  # 0 is reserved and never written
    name: str = Field(min_length=1)
    colour: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    proportions: tuple[float, float, float] = (1.0, 1.0, 1.0)
    radius_um: tuple[float, float]  # plausible profile radii; others are dropped and logged


# C14 (M7-00, approved 2026-11-08); colours Okabe-Ito
DEFAULT_CELL_TYPES = [
    CellType(id=1, name="Neuron", colour="#E69F00", radius_um=(2.0, 15.0)),
    CellType(id=2, name="Astrocyte-like", colour="#56B4E9", radius_um=(2.0, 8.0)),
    CellType(id=3, name="Oligodendrocyte-like", colour="#009E73", radius_um=(1.5, 6.0)),
    CellType(id=4, name="Microglia-like", colour="#F0E442", radius_um=(1.5, 8.0)),
    CellType(id=5, name="Vessel-wall", colour="#D55E00", radius_um=(1.5, 10.0)),
    CellType(id=6, name="Other", colour="#999999", radius_um=(1.5, 15.0)),
]


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
    cell_types: list[CellType] = Field(default_factory=lambda: list(DEFAULT_CELL_TYPES))

    # local alignment at cell scale (contract C9, C15; method approved 2026-11-09, M7-12): votes
    # of nucleus-to-nucleus offsets per window, on top of the pair transform
    align_grid_um: float = Field(default=100.0, gt=0.0)  # node spacing of the displacement field
    align_window_um: float = Field(default=150.0, gt=0.0)  # radius of the nuclei used per node
    align_search_um: float = Field(default=100.0, gt=0.0)  # largest local offset searched
    align_min_peak_ratio: float = Field(default=2.0, gt=1.0)  # peak vs next-best peak to lock on

    # detection QC (M7-16, approved 2026-11-09; Proposed values, calibrated on real slices 1 to
    # 10: densities 1027 to 1147 per mm², class shares within about 15%, 20 to 40% blurred tiles,
    # folds 0.0 to 2.0% of the tissue). Ratios compare a slice with the median of up to two
    # slices on each side; a ratio r flags values above r x or below 1/r x that median.
    qc_cells_density_ratio: float = Field(default=1.33, gt=1.0)
    qc_cells_class_ratio: float = Field(default=1.5, gt=1.0)  # classes with >= 2% share only
    qc_cells_max_blurred_share: float = Field(default=0.5, ge=0.0, le=1.0)  # of the tiles
    qc_fold_od_ratio: float = Field(default=2.0, gt=1.0)  # x the median tissue optical density
    qc_fold_min_area_mm2: float = Field(default=0.05, ge=0.0)  # smaller dark spots are no fold
    qc_fold_max_share: float = Field(default=0.01, ge=0.0, le=1.0)  # of the tissue area

    @field_validator("cell_types")
    @classmethod
    def _ids_in_order(cls, types: list[CellType]) -> list[CellType]:
        # class_id indexes the class probabilities (C13), so ids must be 1..n in order
        if [t.id for t in types] != list(range(1, len(types) + 1)):
            raise ValueError("cell type ids must be 1, 2, 3, ... in order")
        if len({t.name for t in types}) != len(types):
            raise ValueError("cell type names must be unique")
        if any(not 0 < t.radius_um[0] < t.radius_um[1] for t in types):
            raise ValueError("radius_um must be (min, max) with 0 < min < max")
        return types

    @classmethod
    def load(cls, path: Path) -> "ProjectConfig":
        """Settings from a YAML file. Raises pydantic.ValidationError on missing, unknown or
        invalid settings, and TypeError if the file holds no `name: value` settings at all."""
        values = yaml.safe_load(path.read_text())
        if not isinstance(values, dict):
            raise TypeError(f"{path} holds no settings (expected `name: value` lines)")
        return cls.model_validate(values)
