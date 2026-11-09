"""Cell profile file of one slice (contract C13): `cells/{id}.npz` plus `cells/{id}.json`.

One row per detected profile (the cut surface of a nucleus in this section), in the slice frame
of C1 (µm). Class ids follow the type table C14: 1..n, the column order of `class_prob`; there is
no "unclassified" (decision 0006), so every row has a class and a confidence (its top
probability). The metadata file is written last, so a slice counts as done only when both exist.
"""

import io
import json
from dataclasses import dataclass, fields
from typing import Any

import numpy as np

from brainrec.store.layout import WorkDir
from brainrec.store.stage import write_atomic

SCHEMA = "brainrec.cells/1"
PAIR_SCHEMA = "brainrec.cellpair/1"
PAIR_ARRAYS = ("grid_x_um", "grid_y_um", "dx_um", "dy_um", "node_conf", "links", "link_dist_um")
# the appearance features of M7-18, in this order (C13)
FEATURES = (
    "diameter_um",
    "elongation",
    "solidity",
    "darkness",
    "texture",
    "nucleolus",
    "cell_body",
    "halo",
    "vessel_um",
)
MAX_OUTLINE_VERTICES = 32
OUTLINE_UNIT_UM = (
    0.1  # outline vertices are stored as int16 multiples of this, relative to the centre
)
REQUIRED_META = ("source", "detector", "detector_version", "downsample", "mpp_um", "classifier",
                 "class_names")  # fmt: skip

_DTYPES: dict[str, Any] = {
    "x_um": np.float64,
    "y_um": np.float64,
    "r_um": np.float32,
    "area_um2": np.float32,
    "outline_offsets": np.int64,
    "outline_xy": np.int16,
    "features": np.float32,
    "nucleolus_seen": np.uint8,
    "det_conf": np.float32,
    "blurred": np.uint8,
    "class_id": np.uint8,
    "class_prob": np.float32,
    "confidence": np.float32,
}


@dataclass
class Profiles:
    """Profiles of one slice (C13). `features` is (n, 9) in FEATURES order; `class_prob` is
    (n, n_classes) in C14 order; `outline_offsets` (n + 1) index `outline_xy` (m, 2)."""

    x_um: np.ndarray
    y_um: np.ndarray
    r_um: np.ndarray
    area_um2: np.ndarray
    outline_offsets: np.ndarray
    outline_xy: np.ndarray
    features: np.ndarray
    nucleolus_seen: np.ndarray
    det_conf: np.ndarray
    blurred: np.ndarray
    class_id: np.ndarray
    class_prob: np.ndarray
    confidence: np.ndarray

    def __post_init__(self) -> None:
        for f in fields(self):
            setattr(self, f.name, np.asarray(getattr(self, f.name), dtype=_DTYPES[f.name]))

    def __len__(self) -> int:
        return len(self.x_um)

    @classmethod
    def empty(cls, n_classes: int) -> "Profiles":
        z = np.zeros(0)
        return cls(z, z, z, z, np.zeros(1), np.zeros((0, 2)), np.zeros((0, len(FEATURES))),
                   z, z, z, z, np.zeros((0, n_classes)), z)  # fmt: skip

    def feature(self, name: str) -> np.ndarray:
        return self.features[:, FEATURES.index(name)]

    def outline(self, i: int) -> np.ndarray:
        """Outline vertices of profile `i` in µm (slice frame), (k, 2)."""
        a, b = self.outline_offsets[i], self.outline_offsets[i + 1]
        rel = self.outline_xy[a:b].astype(float) * OUTLINE_UNIT_UM
        return rel + [self.x_um[i], self.y_um[i]]

    def validate(self, n_classes: int) -> None:
        """Raise ValueError if the arrays do not form a valid C13 table."""
        n = len(self)
        for name in ("y_um", "r_um", "area_um2", "nucleolus_seen", "det_conf", "blurred",
                     "class_id", "confidence"):  # fmt: skip
            if getattr(self, name).shape != (n,):
                raise ValueError(f"{name} has shape {getattr(self, name).shape}, expected ({n},)")
        if self.features.shape != (n, len(FEATURES)):
            raise ValueError(
                f"features has shape {self.features.shape}, expected ({n}, {len(FEATURES)})"
            )
        if self.class_prob.shape != (n, n_classes):
            raise ValueError(
                f"class_prob has shape {self.class_prob.shape}, expected ({n}, {n_classes})"
            )
        off = self.outline_offsets
        if (
            off.shape != (n + 1,)
            or off[0] != 0
            or off[-1] != len(self.outline_xy)
            or np.any(np.diff(off) < 0)
        ):
            raise ValueError("outline_offsets do not index outline_xy")
        if np.any(np.diff(off) > MAX_OUTLINE_VERTICES):
            raise ValueError(f"an outline has more than {MAX_OUTLINE_VERTICES} vertices")
        if n == 0:
            return
        if self.class_id.min() < 1 or self.class_id.max() > n_classes:
            raise ValueError(f"class ids must be 1..{n_classes} (no unclassified, decision 0006)")
        if not np.array_equal(self.class_id, self.class_prob.argmax(axis=1) + 1):
            raise ValueError("class_id is not the most probable class")
        if not np.allclose(self.confidence, self.class_prob.max(axis=1), atol=1e-6):
            raise ValueError("confidence is not the top class probability")


def encode_outlines(
    outlines: list[np.ndarray], x_um: np.ndarray, y_um: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """(offsets, xy) for absolute outlines in µm: at most MAX_OUTLINE_VERTICES vertices each
    (evenly subsampled), relative to the centres, in int16 units of OUTLINE_UNIT_UM."""
    offsets = [0]
    parts = []
    for poly, x, y in zip(outlines, x_um, y_um, strict=True):
        poly = np.asarray(poly, float).reshape(-1, 2)
        if len(poly) > MAX_OUTLINE_VERTICES:
            idx = np.round(np.linspace(0, len(poly), MAX_OUTLINE_VERTICES, endpoint=False)).astype(
                int
            )
            poly = poly[idx]
        parts.append(np.round((poly - [x, y]) / OUTLINE_UNIT_UM).astype(np.int16))
        offsets.append(offsets[-1] + len(poly))
    xy = np.vstack(parts) if parts else np.zeros((0, 2), np.int16)
    return np.array(offsets, np.int64), xy


def save_profiles(work: WorkDir, slice_id: int, profiles: Profiles, meta: dict[str, Any]) -> None:
    """Write `cells/{id}.npz`, then `cells/{id}.json` (C13), both atomically. `meta` must hold
    REQUIRED_META; schema, slice id and count are added. Raises ValueError on an invalid table."""
    missing = [k for k in REQUIRED_META if k not in meta]
    if missing:
        raise ValueError(f"metadata misses {missing}")
    profiles.validate(len(meta["class_names"]))
    buf = io.BytesIO()
    arrays = {f.name: getattr(profiles, f.name) for f in fields(profiles) if f.name != "features"}
    arrays.update({name: profiles.features[:, j] for j, name in enumerate(FEATURES)})
    np.savez_compressed(buf, **arrays)
    write_atomic(work.cell_profiles(slice_id), buf.getvalue())
    full = {"schema": SCHEMA, "slice_id": slice_id, **meta, "n_profiles": len(profiles)}
    write_atomic(work.cell_meta(slice_id), json.dumps(full, indent=2).encode())


def load_profiles(work: WorkDir, slice_id: int) -> tuple[Profiles, dict[str, Any]]:
    """The profiles and metadata of a slice. FileNotFoundError if the slice has no complete
    cell file (metadata missing); ValueError on another schema or an inconsistent file."""
    meta_path = work.cell_meta(slice_id)
    if not meta_path.exists():
        raise FileNotFoundError(f"slice {slice_id} has no cell file (run detect)")
    meta = json.loads(meta_path.read_text())
    if meta.get("schema") != SCHEMA:
        raise ValueError(f"{meta_path}: unsupported schema {meta.get('schema')!r}")
    with np.load(work.cell_profiles(slice_id)) as z:
        data = {k: z[k] for k in z.files}
    data["features"] = np.stack([data.pop(name) for name in FEATURES], axis=1).reshape(
        -1, len(FEATURES)
    )
    profiles = Profiles(**data)
    if len(profiles) != meta["n_profiles"]:
        raise ValueError(
            f"slice {slice_id}: {len(profiles)} profiles, metadata says {meta['n_profiles']}"
        )
    profiles.validate(len(meta["class_names"]))
    return profiles, meta


def save_cell_pair(work: WorkDir, moving_id: int, arrays: dict[str, np.ndarray],
                   meta: dict[str, Any]) -> None:  # fmt: skip
    """Write `cells/pair_{k}.npz` (the PAIR_ARRAYS of C15), then `cells/pair_{k}.json`, both
    atomically. `grid_x_um` (nx), `grid_y_um` (ny): node positions in the frame of slice k-1;
    `dx_um`, `dy_um`, `node_conf`: (ny, nx); `links` (m, 2) int32 and `link_dist_um` (m)."""
    missing = [k for k in PAIR_ARRAYS if k not in arrays]
    if missing:
        raise ValueError(f"pair arrays miss {missing}")
    ny, nx = len(arrays["grid_y_um"]), len(arrays["grid_x_um"])
    for name in ("dx_um", "dy_um", "node_conf"):
        if np.shape(arrays[name]) != (ny, nx):
            raise ValueError(f"{name} has shape {np.shape(arrays[name])}, expected ({ny}, {nx})")
    buf = io.BytesIO()
    data: dict[str, Any] = {k: np.asarray(arrays[k]) for k in PAIR_ARRAYS}
    np.savez_compressed(buf, **data)
    write_atomic(work.cell_pair(moving_id), buf.getvalue())
    if "fixed" not in meta:
        raise ValueError("pair metadata misses the fixed slice id")
    full = {"schema": PAIR_SCHEMA, "moving": moving_id, **meta}
    write_atomic(work.cell_pair_meta(moving_id), json.dumps(full, indent=2).encode())


def load_cell_pair(work: WorkDir, moving_id: int) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """The arrays and metadata of `cells/pair_{k}`. FileNotFoundError if the metadata is missing;
    ValueError on another schema."""
    meta_path = work.cell_pair_meta(moving_id)
    if not meta_path.exists():
        raise FileNotFoundError(f"pair {moving_id - 1}<-{moving_id} has no alignment (run cells)")
    meta = json.loads(meta_path.read_text())
    if meta.get("schema") != PAIR_SCHEMA:
        raise ValueError(f"{meta_path}: unsupported schema {meta.get('schema')!r}")
    with np.load(work.cell_pair(moving_id)) as z:
        return {k: z[k] for k in PAIR_ARRAYS}, meta
