"""Cell table and QuPath export (M7-14; contract C18).

- `cells/table.csv`: one row per 3D cell (C16) with its class, the mean class probabilities of
  its profiles, its position in the volume, its size, and the source position and measurements of
  its representative (largest) profile. NaN marks values that do not exist (no detector
  confidence; no 3D radius while the section thickness is unknown).
- `cells/qupath/{id}.geojson`: every profile of one slice as a QuPath detection (outline, the
  class of its cell, measurements), so a slice's detections can be checked on the original slide.

Pixel coordinates (`x_px0`, `y_px0`, the QuPath outlines) follow QuPath: level 0 of the source
slide, pixel corners at whole numbers, so x_px = x_um / mpp (C1 puts pixel centres at whole
numbers instead; the two differ by half a pixel).
"""

import json
import math
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from brainrec.cells.link import Cells
from brainrec.cells.place import Placed
from brainrec.store.cells import FEATURES, Profiles

# for the space check: measured on slices 1 to 10 (218 bytes per row, 1,100 per detection),
# plus a margin
TABLE_BYTES_PER_ROW = 300
QUPATH_BYTES_PER_PROFILE = 1400
CHUNK = 200_000
SPACE_MARGIN = 1.2


@dataclass
class TableInput:
    """What the table needs besides the placed cells: per cell, the representative profile's
    position in its slice (µm), its level-0 pixel size (µm) and its 9 features."""

    x_slice_um: np.ndarray
    y_slice_um: np.ndarray
    mpp_um: np.ndarray
    features: np.ndarray  # (n_cells, 9), FEATURES order


def table_columns(class_names: list[str]) -> list[str]:
    return [
        "cell_id", "class", "confidence", *[f"prob_{n}" for n in class_names],
        "x_um", "y_um", "z_um", "radius_um", "radius_3d_um", "n_profiles",
        "slice", "x_slice_um", "y_slice_um", "x_px0", "y_px0",
        *FEATURES, "nucleolus_seen", "blurred", "det_conf",
    ]  # fmt: skip


def check_space(path: Path, needed_bytes: int) -> None:
    """OSError if the drive of `path` has less than SPACE_MARGIN x `needed_bytes` free."""
    path.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path.parent).free
    if free < SPACE_MARGIN * needed_bytes:
        raise OSError(f"not enough free space for {path.name}: about "
                      f"{needed_bytes / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free")  # fmt: skip


def _rows(placed: Placed, cells: Cells, extra: TableInput, names: list[str]) -> Iterator[str]:
    n_cls = len(names)
    fmt = ",".join(["%d", "%s", "%.4f", *["%.4f"] * n_cls, "%.2f", "%.2f", "%.2f", "%.3f",
                    "%.3f", "%d", "%d", "%.2f", "%.2f", "%.1f", "%.1f", *["%.4g"] * len(FEATURES),
                    "%d", "%d", "%.4f"])  # fmt: skip
    class_name = np.array(names, dtype=object)
    for a in range(0, len(placed), CHUNK):
        s = slice(a, a + CHUNK)
        cols = [
            placed.cell_id[s].astype(np.int64),
            class_name[placed.class_id[s].astype(int) - 1],
            placed.confidence[s],
            *cells.class_prob[s].T,
            *placed.centre[s].T,
            placed.radius_um[s],
            cells.radius_3d_um[s],
            placed.n_profiles[s],
            placed.slice_id[s],
            extra.x_slice_um[s],
            extra.y_slice_um[s],
            extra.x_slice_um[s] / extra.mpp_um[s],
            extra.y_slice_um[s] / extra.mpp_um[s],
            *extra.features[s].T,
            placed.nucleolus[s],
            placed.blurred[s],
            placed.det_conf[s],
        ]
        lists = [np.asarray(c).tolist() for c in cols]
        yield "".join(fmt % row + "\n" for row in zip(*lists, strict=True))


def write_table(
    out: Path, placed: Placed, cells: Cells, extra: TableInput, class_names: list[str]
) -> int:
    """Write `cells/table.csv` (C18), one row per cell, in the order of `placed` (which must be
    the cells of `cells`, row for row). Written to a temporary file, then renamed. Returns the
    number of bytes. OSError if the drive lacks space."""
    if not len(placed) == len(cells) == len(extra.x_slice_um):
        raise ValueError("placed cells, cells and table input differ in length")
    check_space(out, len(placed) * TABLE_BYTES_PER_ROW)
    tmp = out.with_name(out.name + ".tmp")
    with tmp.open("w", newline="") as f:
        f.write(",".join(table_columns(class_names)) + "\n")
        for block in _rows(placed, cells, extra, class_names):
            f.write(block)
    os.replace(tmp, out)
    return out.stat().st_size


def _orient(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    return np.sign((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                   - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]))  # fmt: skip


def is_simple(ring: np.ndarray) -> bool:
    """Whether a polygon (n, 2), without the closing point, is simple: at least 3 vertices and
    no two non-adjacent edges touching or crossing (a vertex visited twice makes two of them
    touch). Consecutive duplicate vertices are ignored."""
    pts = np.asarray(ring, float).reshape(-1, 2)
    pts = pts[np.any(pts != np.roll(pts, 1, axis=0), axis=1)]
    n = len(pts)
    if n < 3:
        return False
    a, b = pts, np.roll(pts, -1, axis=0)
    i, j = np.triu_indices(n, 2)
    keep = ~((i == 0) & (j == n - 1))  # the last edge is adjacent to the first
    p, q, r, s = a[i[keep]], b[i[keep]], a[j[keep]], b[j[keep]]
    o1, o2, o3, o4 = _orient(p, q, r), _orient(p, q, s), _orient(r, s, p), _orient(r, s, q)
    collinear = (o1 == 0) & (o2 == 0)
    overlap = (np.maximum(np.minimum(p, q), np.minimum(r, s))
               <= np.minimum(np.maximum(p, q), np.maximum(r, s))).all(axis=1)  # fmt: skip
    return not np.where(collinear, overlap, (o1 * o2 <= 0) & (o3 * o4 <= 0)).any()


def _octagon(x: float, y: float, r: float) -> np.ndarray:
    a = np.linspace(0, 2 * np.pi, 8, endpoint=False)
    return np.c_[x + r * np.cos(a), y + r * np.sin(a)]


def valid_ring(pts_px: np.ndarray) -> tuple[np.ndarray, bool]:
    """A polygon QuPath accepts: the outline rounded to 0.1 pixel, or its convex hull if it is
    not simple (outlines thinned to 32 vertices can fold over themselves, and QuPath rejects a
    whole file for one such polygon: "Reduction failed, possible invalid input"). Returns the
    ring without the closing point, and whether it was replaced."""
    ring = np.round(np.asarray(pts_px, float).reshape(-1, 2), 1)
    if is_simple(ring):
        return ring, False
    # float32 for OpenCV is exact to 0.016 px below 262,144 px (slides are up to about 150,000)
    hull = cv2.convexHull(ring.astype(np.float32)).reshape(-1, 2)
    return np.round(hull.astype(float), 1), True


def _ring(p: Profiles, i: int, mpp_um: float, counts: dict[str, int]) -> list[list[float]]:
    pts = p.outline(i)
    if len(pts) < 3:  # no usable outline: an octagon of the profile's radius
        pts = _octagon(float(p.x_um[i]), float(p.y_um[i]), float(p.r_um[i]))
    ring, replaced = valid_ring(pts / mpp_um)
    if replaced and len(ring) < 3:  # a hull of collinear points: the octagon instead
        ring = np.round(_octagon(float(p.x_um[i]), float(p.y_um[i]), float(p.r_um[i])) / mpp_um, 1)
    counts["convex_hull"] = counts.get("convex_hull", 0) + int(replaced)
    out = ring.tolist()
    return [*out, out[0]]


def _measure(values: dict[str, float]) -> dict[str, float]:
    """Measurements without NaN, which QuPath's JSON reader rejects."""
    return {k: v for k, v in values.items() if not math.isnan(v)}


def qupath_features(
    p: Profiles,
    mpp_um: float,
    cell_id: np.ndarray,
    cell_class: list[str],
    cell_conf: np.ndarray,
    cell_profiles: np.ndarray,
    colours: dict[str, tuple[int, int, int]],
    counts: dict[str, int] | None = None,
) -> Iterator[dict]:
    """Every profile of one slice as a QuPath detection: outline (level-0 pixels, made valid
    with `valid_ring`), the class of its cell, and measurements. The `cell_*` arrays are per
    profile (the profile's cell). `counts["convex_hull"]` counts the outlines replaced."""
    counts = {} if counts is None else counts
    for i in range(len(p)):
        cls = cell_class[i]
        values = {
            "cell_id": float(cell_id[i]),
            "cell confidence": float(cell_conf[i]),
            "profiles in cell": float(cell_profiles[i]),
            "radius_um": float(p.r_um[i]),
            "area_um2": float(p.area_um2[i]),
            **{name: float(v) for name, v in zip(FEATURES, p.features[i], strict=True)},
            "nucleolus_seen": float(p.nucleolus_seen[i]),
            "blurred": float(p.blurred[i]),
            "det_conf": float(p.det_conf[i]),
        }
        yield {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [_ring(p, i, mpp_um, counts)]},
            "properties": {
                "objectType": "detection",
                "classification": {"name": cls, "color": list(colours[cls])},
                "measurements": _measure(values),
            },
        }


def write_qupath(out: Path, features: Iterator[dict], n_profiles: int) -> int:
    """Stream the detections into `cells/qupath/{id}.geojson` (a FeatureCollection), via a
    temporary file. Returns the number of bytes. OSError if the drive lacks space."""
    check_space(out, n_profiles * QUPATH_BYTES_PER_PROFILE)
    tmp = out.with_name(out.name + ".tmp")
    with tmp.open("w") as f:
        f.write('{"type": "FeatureCollection", "features": [\n')
        for k, feat in enumerate(features):
            f.write((",\n" if k else "") + json.dumps(feat, separators=(",", ":")))
        f.write("\n]}\n")
    os.replace(tmp, out)
    return out.stat().st_size
