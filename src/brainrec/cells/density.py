"""Density maps per cell class (M7-15; contract C18, F21).

`density.zarr`: OME-Zarr 0.5, axes c (class), z (section), y, x; x and y on a grid of
`density_grid_um` in the volume frame (C11, C16), z one plane per section, so the map lies on the
volume and its cells. Per section, every profile is counted at its centre under its cell's class
(C16); the counts and the tissue area of each grid square (the section's tissue mask, C4, moved
into the volume frame) are both smoothed with a Gaussian of σ = 1 grid square, and their ratio
is the density (normalised convolution, so the tissue edge does not dilute it). Squares where less
than half of the smoothed neighbourhood is tissue stay 0.

Units: profiles per mm² of section while `section_thickness_um` is unknown; once it is set,
cells per mm³ by the Abercrombie correction N_v = N_a / (T + D), with T the section thickness and
D the mean nucleus diameter of the class. Profiles are counted rather than linked cells, since
linking finds only part of the continuations (M7-13).
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import zarr
from scipy.ndimage import gaussian_filter
from zarr.codecs import ZstdCodec

from brainrec.registration import Transform2D

SUBSAMPLE = 5  # tissue fraction of a grid square from 5 x 5 samples
MIN_TISSUE = 0.5  # smoothed tissue fraction below which a square stays 0
SIGMA = 1.0  # Gaussian smoothing in grid squares (C18)
CHUNK = 128
SHARD_SECTIONS = 10  # sections per shard file (exFAT: few files)
UNIT_AREA = "profiles/mm2"
UNIT_VOLUME = "cells/mm3"


def tissue_fraction(
    mask: np.ndarray,
    mask_um_px: float,
    to_volume: Transform2D,
    shape: tuple[int, int],
    grid_um: float,
) -> np.ndarray:
    """Share of each grid square (ny, nx) covered by tissue. `mask` is a slice's tissue mask
    (C4) with `mask_um_px` µm per pixel; `to_volume` maps slice µm to volume µm (the chained
    transform minus the canvas origin). Pixel centres follow C1: pixel j covers
    [j, j + 1) x size."""
    fine = grid_um / SUBSAMPLE
    s1 = np.array([[mask_um_px, 0, mask_um_px / 2], [0, mask_um_px, mask_um_px / 2], [0, 0, 1]])
    s2 = np.array([[1 / fine, 0, -0.5], [0, 1 / fine, -0.5], [0, 0, 1]])
    m = (s2 @ to_volume.matrix3 @ s1)[:2]
    ny, nx = shape
    src = (mask > 0).astype(np.float32)
    warped = cv2.warpAffine(src, m, (nx * SUBSAMPLE, ny * SUBSAMPLE), flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=0)  # fmt: skip
    return warped.reshape(ny, SUBSAMPLE, nx, SUBSAMPLE).mean(axis=(1, 3))


def section_density(
    xy_um: np.ndarray, class_id: np.ndarray, n_classes: int, tissue: np.ndarray, grid_um: float
) -> np.ndarray:
    """Profiles per mm² of section, (n_classes, ny, nx), from the profile centres of one section
    (volume µm) and their class ids (1..n), over the tissue fraction `tissue` (ny, nx)."""
    ny, nx = tissue.shape
    counts = np.zeros((n_classes, ny, nx))
    ij = np.floor(np.asarray(xy_um, float).reshape(-1, 2) / grid_um).astype(int)
    ok = (ij[:, 0] >= 0) & (ij[:, 1] >= 0) & (ij[:, 0] < nx) & (ij[:, 1] < ny)
    np.add.at(counts, (np.asarray(class_id)[ok].astype(int) - 1, ij[ok, 1], ij[ok, 0]), 1.0)
    area = gaussian_filter(tissue * grid_um**2 / 1e6, SIGMA, mode="constant")
    near = gaussian_filter(tissue, SIGMA, mode="constant")
    valid = (tissue > 0) & (near >= MIN_TISSUE)
    out = np.zeros_like(counts)
    for c in range(n_classes):
        smooth = gaussian_filter(counts[c], SIGMA, mode="constant")
        out[c][valid] = smooth[valid] / area[valid]
    return out.astype(np.float32)


def abercrombie(per_mm2: np.ndarray, thickness_um: float, diameters_um: np.ndarray) -> np.ndarray:
    """Cells per mm³ from profiles per mm² (classes first): N_v = N_a / (T + D) per class."""
    d = np.asarray(diameters_um, float).reshape(-1, *([1] * (per_mm2.ndim - 1)))
    return (per_mm2 / ((thickness_um + d) / 1000.0)).astype(np.float32)


@dataclass
class DensityMeta:
    unit: str
    class_names: list[str]
    slice_ids: list[int]
    grid_um: float
    z_spacing_um: float
    section_thickness_um: float | None
    diameters_um: list[float] | None


def density_ome(grid_um: float, z_spacing_um: float) -> dict:
    """OME-Zarr 0.5 metadata: axes c, z, y, x; voxel i's centre at (i + 0.5) x scale (C11)."""
    scale = [1.0, z_spacing_um, grid_um, grid_um]
    return {
        "version": "0.5",
        "multiscales": [{
            "name": "density",
            "axes": [{"name": "c", "type": "channel"},
                     {"name": "z", "type": "space", "unit": "micrometer"},
                     {"name": "y", "type": "space", "unit": "micrometer"},
                     {"name": "x", "type": "space", "unit": "micrometer"}],
            "datasets": [{"path": "0", "coordinateTransformations": [
                {"type": "scale", "scale": scale},
                {"type": "translation", "translation": [0.0] + [v / 2 for v in scale[1:]]}]}],
        }],
    }  # fmt: skip


def write_density(out: Path, values: np.ndarray, meta: DensityMeta) -> int:
    """Write `values` (classes, sections, ny, nx) as `density.zarr` (C18) via `<out>.tmp`, then
    rename. Every chunk holds all classes of its squares: Neuroglancer reads a channel axis only
    when it is not split across chunks (M7-15: chunks of one class were never requested). One
    shard file per SHARD_SECTIONS sections. Returns the size in bytes. The 99th percentile of
    each class inside the tissue is stored for the viewer's colour scale."""
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    n_cls, nz, ny, nx = values.shape
    root = zarr.open_group(str(tmp), mode="w", zarr_format=3)
    arr = root.create_array(
        "0",
        shape=values.shape,
        dtype="float32",
        chunks=(n_cls, 1, CHUNK, CHUNK),
        shards=(n_cls, min(nz, SHARD_SECTIONS), -(-ny // CHUNK) * CHUNK, -(-nx // CHUNK) * CHUNK),
        fill_value=0.0,
        compressors=ZstdCodec(level=3),
    )
    for z0 in range(0, nz, SHARD_SECTIONS):
        arr[:, z0 : z0 + SHARD_SECTIONS] = values[:, z0 : z0 + SHARD_SECTIONS]
    p99 = [float(np.percentile(v[v > 0], 99)) if (v > 0).any() else 0.0 for v in values]
    root.attrs["ome"] = density_ome(meta.grid_um, meta.z_spacing_um)
    root.attrs["brainrec"] = {
        "unit": meta.unit, "class_names": meta.class_names, "slice_ids": meta.slice_ids,
        "grid_um": meta.grid_um, "section_thickness_um": meta.section_thickness_um,
        "diameters_um": meta.diameters_um, "p99": p99,
    }  # fmt: skip
    if out.exists():
        shutil.rmtree(out)
    os.replace(tmp, out)
    return sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
