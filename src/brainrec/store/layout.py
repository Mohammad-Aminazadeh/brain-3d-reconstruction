"""File layout of the work directory. The only place that knows these names.

Names follow contracts C4 (preprocessing files), C5 (pairs and reference) and C10 (QC report):

    manifest.json
    volume.zarr/
    reg/{id}.png, reg/{id}.json      registration image and its metadata
    mask/{id}.png                    tissue mask
    transforms/pair_{k}.json         PairResult of slice k onto slice k-1
    transforms/chained_{k}.json      transform of slice k onto the reference slice
    qc/report.json, qc/overlay_{k}.png
"""

from dataclasses import dataclass
from pathlib import Path


def _check_id(slice_id: int) -> int:
    if isinstance(slice_id, bool) or not isinstance(slice_id, int) or slice_id < 1:
        raise ValueError(f"slice id must be a positive integer, got {slice_id!r}")
    return slice_id


@dataclass(frozen=True)
class WorkDir:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def volume(self) -> Path:
        return self.root / "volume.zarr"

    # preprocessing (C4)
    @property
    def reg_dir(self) -> Path:
        return self.root / "reg"

    @property
    def mask_dir(self) -> Path:
        return self.root / "mask"

    def reg_image(self, slice_id: int) -> Path:
        return self.reg_dir / f"{_check_id(slice_id)}.png"

    def reg_meta(self, slice_id: int) -> Path:
        return self.reg_dir / f"{_check_id(slice_id)}.json"

    def mask(self, slice_id: int) -> Path:
        return self.mask_dir / f"{_check_id(slice_id)}.png"

    # registration (C5)
    @property
    def transforms_dir(self) -> Path:
        return self.root / "transforms"

    def pair_transform(self, moving_id: int) -> Path:
        """PairResult of slice `moving_id` onto the previous slice."""
        return self.transforms_dir / f"pair_{_check_id(moving_id)}.json"

    def chained_transform(self, slice_id: int) -> Path:
        """Transform of slice `slice_id` onto the reference slice."""
        return self.transforms_dir / f"chained_{_check_id(slice_id)}.json"

    # quality control (C10)
    @property
    def qc_dir(self) -> Path:
        return self.root / "qc"

    @property
    def qc_report(self) -> Path:
        return self.qc_dir / "report.json"

    def qc_overlay(self, moving_id: int) -> Path:
        return self.qc_dir / f"overlay_{_check_id(moving_id)}.png"
