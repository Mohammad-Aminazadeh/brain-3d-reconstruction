"""Stack slices into a multi-resolution OME-Zarr volume.

Two modes (contract C8):

- **centred** (M2; no transforms): slices placed centred on a shared canvas, each level read
  straight from the slide's own pyramid at downsample `base_downsample * 2**k` (no resampling);
  offsets are multiples of 2**(n_levels-1) base pixels so all levels stay aligned.
- **registered** (M3-09; transforms and masks): each slice resampled through its transform
  (slice -> reference, µm) into a canvas in the reference frame, **tissue only** (C11): voxels
  outside the slice's mask, enlarged by `mask_margin_um`, are background (255, not stored);
  the canvas is the extent of all transformed, enlarged masks, aligned to the pyramid grid.
  Each output tile is one shard and is resampled from the slide's own pyramid level of the
  same resolution; tiles without tissue are never read.

Layout: OME-Zarr 0.5 on Zarr v3, axes (c, z, y, x), uint8, one array per pyramid level named
"0", "1", ... Chunks are grouped into shard files (few large files instead of many small ones:
the external drive is exFAT with 512 KB blocks).
"""

import hashlib
import json
import logging
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import zarr
from zarr.codecs import ZstdCodec

from brainrec.catalog import Catalog
from brainrec.registration import Transform2D
from brainrec.slides import SliceSource, SlideReader
from brainrec.store import Manifest
from brainrec.volume.warp import (
    canvas_tiles,
    enlarge_mask,
    finish_tile,
    outline_corners,
    source_box,
    tissue_extent_um,
    warp_mask_tile,
    warp_tile,
)

log = logging.getLogger(__name__)

FORMAT = "ome-zarr-0.5-sharded"


class VolumeExistsError(RuntimeError):
    """An existing volume was built with different settings and would be overwritten."""


@dataclass(frozen=True)
class VolumeSpec:
    z_spacing_um: float
    base_downsample: int = 4
    n_levels: int = 5
    chunk_xy: int = 512  # unit fetched by the viewer
    shard_xy: int = 4096  # unit stored as one file; also the band or tile size written at once
    mask_margin_um: float = 50.0  # registered mode: margin around the tissue mask (C11, decision 9)

    def __post_init__(self) -> None:
        if self.shard_xy % self.chunk_xy:
            raise ValueError("shard_xy must be a multiple of chunk_xy")


def _roundup(v: int, m: int) -> int:
    return -(-v // m) * m


def aligned_bands(start: int, stop: int, step: int) -> list[tuple[int, int]]:
    """Split [start, stop) at multiples of `step`, so no band crosses a shard boundary."""
    bands = []
    y = start
    while y < stop:
        end = min(stop, (y // step + 1) * step)
        bands.append((y, end))
        y = end
    return bands


def _grid(dim: int, chunk_xy: int, shard_xy: int) -> tuple[int, int]:
    """(chunk, shard) edge for one axis; the shard is always a multiple of the chunk."""
    if dim <= chunk_xy:
        return dim, dim
    return chunk_xy, min(shard_xy, _roundup(dim, chunk_xy))


def _open(
    out: Path, manifest: Manifest | None, signature: dict[str, Any], overwrite: bool
) -> tuple[zarr.Group, list[int], bool]:
    """Open `out` for resuming (same signature) or create it afresh; returns (root, done, resume)."""
    state = manifest.get("volume") if manifest else None
    resume = state is not None and state["signature"] == signature and out.exists()
    if out.exists() and not resume and not overwrite:
        raise VolumeExistsError(
            f"{out} exists with different settings or unknown state; use overwrite to replace it"
        )
    done: list[int] = list(state["done"]) if resume and state else []
    root = zarr.open_group(str(out), mode="r+" if resume else "w", zarr_format=3)
    if not resume and manifest:
        manifest.set("volume", {"signature": signature, "done": []})
    return root, done, resume


def _create(
    root: zarr.Group,
    spec: VolumeSpec,
    shape_hw: tuple[int, int],
    n_slices: int,
    mpp: tuple[float, float],
    factors: list[int],
) -> None:
    """Pyramid arrays (white fill, sharded, zstd) and the OME-Zarr 0.5 metadata."""
    H0, W0 = shape_hw
    for k in range(spec.n_levels):
        h, w = H0 // 2**k, W0 // 2**k
        (cy, sy), (cx, sx) = (
            _grid(h, spec.chunk_xy, spec.shard_xy),
            _grid(w, spec.chunk_xy, spec.shard_xy),
        )
        root.create_array(
            str(k),
            shape=(3, n_slices, h, w),
            chunks=(3, 1, cy, cx),
            shards=(3, 1, sy, sx),
            dtype="uint8",
            fill_value=255,
            compressors=ZstdCodec(level=3),
        )
    root.attrs["ome"] = {
        "version": "0.5",
        "multiscales": [
            {
                "name": "volume",
                "axes": [
                    {"name": "c", "type": "channel"},
                    {"name": "z", "type": "space", "unit": "micrometer"},
                    {"name": "y", "type": "space", "unit": "micrometer"},
                    {"name": "x", "type": "space", "unit": "micrometer"},
                ],
                "datasets": [
                    {
                        "path": str(k),
                        "coordinateTransformations": [
                            {
                                "type": "scale",
                                "scale": [1.0, spec.z_spacing_um, mpp[1] * f, mpp[0] * f],
                            }
                        ],
                    }
                    for k, f in enumerate(factors)
                ],
            }
        ],
    }


def _level_array(root: zarr.Group, k: int) -> tuple[zarr.Array, tuple[int, int]]:
    """Level k's array and its shard size (y, x)."""
    arr = root[str(k)]
    assert isinstance(arr, zarr.Array) and arr.shards is not None
    return arr, (arr.shards[2], arr.shards[3])


def build_volume(
    catalog: Catalog,
    out: Path,
    spec: VolumeSpec,
    manifest: Manifest | None = None,
    reader_factory: Callable[[Path], SliceSource] = SlideReader.open,
    overwrite: bool = False,
    transforms: dict[int, Transform2D] | None = None,
    masks: dict[int, Path] | None = None,
) -> Path:
    """Write `out` (an OME-Zarr directory) and return it.

    Without `transforms`/`masks`: centred mode (M2). With both (keyed by slice id;
    `transforms[i]` maps slice i -> reference in µm, `masks[i]` is its tissue mask PNG at one of
    the slide's pyramid levels): registered, tissue-only mode (C8, C11).

    Resumes per slice via `manifest` when the inputs are unchanged. If `out` exists but was
    built from other inputs, raises VolumeExistsError unless `overwrite` is True.
    """
    if (transforms is None) != (masks is None):
        raise ValueError("transforms and masks must be given together (contract C8)")
    if transforms is not None and masks is not None:
        return _build_registered(
            catalog, out, spec, manifest, reader_factory, overwrite, transforms, masks
        )
    factors = [spec.base_downsample * 2**k for k in range(spec.n_levels)]

    # Pass 1: headers only.
    dims: dict[int, list[tuple[int, int]]] = {}
    mpps: set[tuple[float, float]] = set()
    for s in catalog.slices:
        r = reader_factory(s.path)
        try:
            lv = [r.levels[r.level_for_downsample(f)] for f in factors]
            dims[s.id] = [(x.width, x.height) for x in lv]
            mpps.add(r.mpp)
        finally:
            r.close()
    if len(mpps) != 1:
        raise ValueError(f"slices have different pixel sizes: {sorted(mpps)}")
    mpp = mpps.pop()

    align = 2 ** (spec.n_levels - 1)
    W0 = _roundup(max(d[0][0] for d in dims.values()), align)
    H0 = _roundup(max(d[0][1] for d in dims.values()), align)
    offsets = {
        i: (((W0 - d[0][0]) // 2) // align * align, ((H0 - d[0][1]) // 2) // align * align)
        for i, d in dims.items()
    }
    ids = [s.id for s in catalog.slices]
    signature = {"format": FORMAT, "spec": asdict(spec), "ids": ids, "canvas": [W0, H0]}

    root, done, resume = _open(out, manifest, signature, overwrite)
    if not resume:
        _create(root, spec, (H0, W0), len(ids), mpp, factors)
        root.attrs["brainrec"] = {
            "slice_ids": ids,
            "offsets_base_px": {str(i): list(o) for i, o in offsets.items()},
        }

    for zi, s in enumerate(catalog.slices):
        if s.id in done:
            log.info("slice %d: already done, skipping", s.id)
            continue
        r = reader_factory(s.path)
        try:
            for k, f in enumerate(factors):
                arr, (shard_y, _) = _level_array(root, k)
                lvl = r.level_for_downsample(f)
                w, h = dims[s.id][k]
                ox, oy = offsets[s.id][0] // 2**k, offsets[s.id][1] // 2**k
                w = min(w, arr.shape[3] - ox)
                h = min(h, arr.shape[2] - oy)
                for y0, y1 in aligned_bands(oy, oy + h, shard_y):
                    band = r.read_region(lvl, 0, y0 - oy, w, y1 - y0)
                    arr[:, zi, y0:y1, ox : ox + w] = band.transpose(2, 0, 1)
                log.info("slice %d level %d done", s.id, k)
        finally:
            r.close()
        done.append(s.id)
        if manifest:
            manifest.set("volume", {"signature": signature, "done": done})
    return out


# --- registered, tissue-only mode (M3-09) ---------------------------------------------------


@dataclass(frozen=True)
class _Slice:
    mask: np.ndarray  # tissue mask enlarged by the margin, uint8 {0, 255}
    mask_downsample: float  # pyramid level of the mask
    corners: np.ndarray  # outline corners of the enlarged mask, in mask pixels


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _mask_downsample(shape_hw: tuple[int, ...], reader: SliceSource) -> float:
    """The pyramid level whose size equals the mask's (preprocessing writes masks at a level)."""
    for lv in reader.levels:
        if (lv.height, lv.width) == tuple(shape_hw):
            return lv.downsample
    raise ValueError(f"mask of size {tuple(shape_hw)} matches no pyramid level of the slide")


def _build_registered(
    catalog: Catalog,
    out: Path,
    spec: VolumeSpec,
    manifest: Manifest | None,
    reader_factory: Callable[[Path], SliceSource],
    overwrite: bool,
    transforms: dict[int, Transform2D],
    masks: dict[int, Path],
) -> Path:
    ids = [s.id for s in catalog.slices]
    missing = [i for i in ids if i not in transforms or i not in masks]
    if missing:
        raise ValueError(f"no transform or mask for slices {missing}")
    factors = [spec.base_downsample * 2**k for k in range(spec.n_levels)]

    # Pass 1: headers and masks; extent of the transformed, enlarged tissue (M3-09a).
    geo: dict[int, _Slice] = {}
    mpps: set[tuple[float, float]] = set()
    for s in catalog.slices:
        mask = cv2.imread(str(masks[s.id]), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            raise FileNotFoundError(f"tissue mask of slice {s.id} not found: {masks[s.id]}")
        r = reader_factory(s.path)
        try:
            for f in factors:
                r.level_for_downsample(f)  # every output level must exist in the slide
            ds = _mask_downsample(mask.shape, r)
            mpp_s = r.mpp
        finally:
            r.close()
        mpps.add(mpp_s)
        enlarged = enlarge_mask(mask, spec.mask_margin_um / (ds * mpp_s[0]))
        geo[s.id] = _Slice(enlarged, ds, outline_corners(enlarged))
        if not len(geo[s.id].corners):
            log.warning("slice %d: empty tissue mask, its plane stays background", s.id)
    if len(mpps) != 1:
        raise ValueError(f"slices have different pixel sizes: {sorted(mpps)}")
    mpp = mpps.pop()
    extents = [
        e
        for i in ids
        if (e := tissue_extent_um(geo[i].corners, geo[i].mask_downsample, mpp, transforms[i]))
    ]
    if not extents:
        raise ValueError("no tissue in any slice")
    align = 2 ** (spec.n_levels - 1)
    cell = (mpp[0] * spec.base_downsample * align, mpp[1] * spec.base_downsample * align)
    ox = math.floor(min(e[0] for e in extents) / cell[0]) * cell[0]
    oy = math.floor(min(e[1] for e in extents) / cell[1]) * cell[1]
    W0 = math.ceil((max(e[2] for e in extents) - ox) / cell[0]) * align
    H0 = math.ceil((max(e[3] for e in extents) - oy) / cell[1]) * align
    canvas = Transform2D.similarity(0.0, 1.0, -ox, -oy)  # reference µm -> canvas µm

    t_hash = {
        str(i): _sha(json.dumps(transforms[i].to_dict(), sort_keys=True).encode()) for i in ids
    }
    m_hash = {str(i): _sha(Path(masks[i]).read_bytes()) for i in ids}
    signature = {
        "format": FORMAT, "mode": "registered", "spec": asdict(spec), "ids": ids,
        "canvas": [W0, H0], "origin_um": [ox, oy], "transforms": t_hash, "masks": m_hash,
    }  # fmt: skip

    root, done, resume = _open(out, manifest, signature, overwrite)
    if not resume:
        _create(root, spec, (H0, W0), len(ids), mpp, factors)
        identity = Transform2D.identity()
        root.attrs["brainrec"] = {
            "mode": "registered",
            "slice_ids": ids,
            "reference_slice": next((i for i in ids if transforms[i].allclose(identity)), None),
            "canvas_origin_um": [ox, oy],
            "mask_margin_um": spec.mask_margin_um,
            "transform_sha256": t_hash,
            "mask_sha256": m_hash,
            "slice_to_reference_um": {str(i): transforms[i].matrix.tolist() for i in ids},
        }

    # Pass 2: per slice, level and shard-sized tile (M3-09b to f).
    for zi, s in enumerate(catalog.slices):
        g = geo[s.id]
        if s.id in done:
            log.info("slice %d: already done, skipping", s.id)
            continue
        if not len(g.corners):  # empty mask: the plane stays background
            done.append(s.id)
            if manifest:
                manifest.set("volume", {"signature": signature, "done": done})
            continue
        to_canvas = canvas.compose(transforms[s.id])  # slice µm -> canvas µm
        r = reader_factory(s.path)
        try:
            for k, f in enumerate(factors):
                arr, shards = _level_array(root, k)
                lvl = r.level_for_downsample(f)
                lv = r.levels[lvl]
                inv = np.linalg.inv(to_canvas.to_pixels(f, f, mpp))  # canvas px -> slice px
                mask_to_canvas = to_canvas.to_pixels(g.mask_downsample, f, mpp)
                pts = g.corners @ mask_to_canvas[:2, :2].T + mask_to_canvas[:2, 2]
                extent = (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())
                written = 0
                for tile in canvas_tiles(extent, (arr.shape[2], arr.shape[3]), shards):
                    keep = warp_mask_tile(g.mask, mask_to_canvas, tile)
                    if not keep.any():
                        continue  # glass only: nothing read, nothing stored
                    box = source_box(inv, tile, lv.width, lv.height)
                    if box is None:
                        continue
                    sx0, sy0, sx1, sy1 = box
                    region = r.read_region(lvl, sx0, sy0, sx1 - sx0, sy1 - sy0)
                    rgb, valid = warp_tile(region, (sx0, sy0), inv, tile)
                    keep &= valid
                    if not keep.any():
                        continue
                    x0, y0, x1, y1 = tile
                    arr[:, zi, y0:y1, x0:x1] = finish_tile(rgb, keep).transpose(2, 0, 1)
                    written += 1
                log.info("slice %d level %d done (%d tiles)", s.id, k, written)
        finally:
            r.close()
        done.append(s.id)
        if manifest:
            manifest.set("volume", {"signature": signature, "done": done})
    return out
