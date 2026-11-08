"""Placement of cells in the volume frame (M7-07a; contract C16).

Until linking (M7-13) every profile is its own cell (one profile, its own class and confidence).
Position: the profile centre through its slice's chained transform (slice -> reference µm, C2,
C5), minus the volume's canvas origin (C8, C11); z = (index of the slice in the volume + 0.5) x
the volume's z spacing, the centre of its plane. Radius: the profile radius x the transform's
scale, times the class proportions (C14). Cell id: slice id x 2^32 + profile index.
"""

from dataclasses import dataclass

import numpy as np

from brainrec.registration import Transform2D
from brainrec.store.cells import Profiles


@dataclass(frozen=True)
class VolumeFrame:
    """What placement needs from the registered volume (C11 metadata): the slice ids in z order,
    the canvas origin (reference µm), the z spacing (µm) and the slice -> reference transforms
    the volume was built with."""

    slice_ids: tuple[int, ...]
    origin_um: tuple[float, float]
    z_spacing_um: float
    transforms: dict[int, Transform2D]

    def z_um(self, slice_id: int) -> float:
        """Centre of the slice's plane in the volume (µm)."""
        return (self.slice_ids.index(slice_id) + 0.5) * self.z_spacing_um


@dataclass
class Placed:
    """Cells in the volume frame (µm), one row each."""

    cell_id: np.ndarray  # uint64
    class_id: np.ndarray  # uint8, C14
    centre: np.ndarray  # (n, 3) float32: x, y, z
    radii: np.ndarray  # (n, 3) float32: sphere radius x class proportions
    radius_um: np.ndarray  # float32: sphere radius (before proportions)
    confidence: np.ndarray  # float32
    det_conf: np.ndarray  # float32
    slice_id: np.ndarray  # uint16: slice of the representative profile
    n_profiles: np.ndarray  # uint16
    nucleolus: np.ndarray  # uint8
    blurred: np.ndarray  # uint8

    def __len__(self) -> int:
        return len(self.cell_id)

    def subset(self, keep: np.ndarray) -> "Placed":
        return Placed(**{k: v[keep] for k, v in self.__dict__.items()})

    @classmethod
    def concat(cls, parts: list["Placed"]) -> "Placed":
        return cls(**{k: np.concatenate([getattr(p, k) for p in parts])
                      for k in cls.__dataclass_fields__})  # fmt: skip


def place_profiles(
    profiles: Profiles,
    slice_id: int,
    frame: VolumeFrame,
    proportions: np.ndarray,
) -> Placed:
    """Every profile of a slice as its own cell in the volume frame. `proportions` is
    (n_classes, 3), row = class id - 1. KeyError if the volume lacks the slice."""
    t = frame.transforms[slice_id]
    n = len(profiles)
    xy = t.apply(np.stack([profiles.x_um, profiles.y_um], axis=1).reshape(-1, 2))
    xy = xy - np.asarray(frame.origin_um)
    centre = np.column_stack([xy, np.full(n, frame.z_um(slice_id))]).astype(np.float32)
    r = (profiles.r_um.astype(float) * t.scale).astype(np.float32)
    k = profiles.class_id.astype(int) - 1
    radii = (r[:, None] * np.asarray(proportions, float)[k]).astype(np.float32)
    return Placed(
        cell_id=(np.uint64(slice_id) << np.uint64(32)) + np.arange(n, dtype=np.uint64),
        class_id=profiles.class_id.astype(np.uint8),
        centre=centre,
        radii=radii.reshape(n, 3),
        radius_um=r,
        confidence=profiles.confidence.astype(np.float32),
        det_conf=profiles.det_conf.astype(np.float32),
        slice_id=np.full(n, slice_id, np.uint16),
        n_profiles=np.ones(n, np.uint16),
        nucleolus=profiles.nucleolus_seen.astype(np.uint8),
        blurred=profiles.blurred.astype(np.uint8),
    )
