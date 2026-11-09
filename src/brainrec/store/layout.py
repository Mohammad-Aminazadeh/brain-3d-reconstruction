"""File layout of the work directory. The only place that knows these names.

Names follow contracts C4 (preprocessing files), C5 (pairs and reference) and C10 (QC report):

    manifest.json
    volume.zarr/
    reg/{id}.png, reg/{id}.json      registration image and its metadata
    mask/{id}.png                    tissue mask
    transforms/pair_{k}.json         PairResult of slice k onto slice k-1
    transforms/chained_{k}.json      transform of slice k onto the reference slice
    qc/report.json, qc/overlay_{k}.png
    qc/cells_report.json, qc/cells_{id}.jpg   detection QC and review crops (M7-16)
    cells/{id}.npz, cells/{id}.json  cell profiles of a slice and their metadata (C13)
    cells/pair_{k}.npz, cells/pair_{k}.json   local alignment and links of k onto k-1 (C15)
    cells/layer_<name>/              Neuroglancer cell layer per class (C17)
    cells/table.csv, cells/qupath/{id}.geojson, density.zarr   exports (C18)
    labels/                          label crops, drafts and expert labels (M7-02, M7-19)
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

    @property
    def qc_cells_report(self) -> Path:
        return self.qc_dir / "cells_report.json"

    def qc_cells_crops(self, slice_id: int) -> Path:
        """Review crops of one slice's detection, one sheet (M7-16)."""
        return self.qc_dir / f"cells_{_check_id(slice_id)}.jpg"

    # cells (C13, C15, C17, C18)
    @property
    def cells_dir(self) -> Path:
        return self.root / "cells"

    def cell_profiles(self, slice_id: int) -> Path:
        return self.cells_dir / f"{_check_id(slice_id)}.npz"

    def cell_meta(self, slice_id: int) -> Path:
        return self.cells_dir / f"{_check_id(slice_id)}.json"

    def cell_pair(self, moving_id: int) -> Path:
        """Local alignment and links of slice `moving_id` onto the previous slice."""
        return self.cells_dir / f"pair_{_check_id(moving_id)}.npz"

    def cell_pair_meta(self, moving_id: int) -> Path:
        return self.cells_dir / f"pair_{_check_id(moving_id)}.json"

    def cell_layer(self, class_name: str) -> Path:
        """Annotation layer directory of one class, e.g. `layer_astrocyte-like`."""
        name = class_name.strip().lower().replace(" ", "-")
        if not name or "/" in name or name.startswith("."):
            raise ValueError(f"invalid class name {class_name!r}")
        return self.cells_dir / f"layer_{name}"

    @property
    def cell_table(self) -> Path:
        return self.cells_dir / "table.csv"

    def qupath(self, slice_id: int) -> Path:
        return self.cells_dir / "qupath" / f"{_check_id(slice_id)}.geojson"

    @property
    def density(self) -> Path:
        return self.root / "density.zarr"

    @property
    def labels_dir(self) -> Path:
        return self.root / "labels"
