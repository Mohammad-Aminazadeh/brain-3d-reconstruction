"""Similarity transforms between slice frames, in micrometres (contracts C1 to C3).

C1: each slice has a frame in µm whose origin is the top-left corner of level-0 pixel (0, 0),
x right, y down. Pixel coordinates follow OpenCV: integer (x, y) is the centre of column x,
row y. At downsample d: X_um = (x + 0.5) * d * mpp_x, Y_um = (y + 0.5) * d * mpp_y.

C2/C3: `Transform2D` maps the slice frame to the reference frame, p' = A @ [x, y, 1], with A a
2x3 similarity matrix [[s cos t, -s sin t, tx], [s sin t, s cos t, ty]], s > 0, t in radians
(y down, so positive t turns clockwise on screen). Family "rigid" is the special case s = 1.
"""

import json
import math
from pathlib import Path
from typing import Literal

import numpy as np

SCHEMA = "brainrec.transform/1"
Family = Literal["rigid", "similarity"]
_RTOL = 1e-8  # relative tolerance for the similarity structure (float round-off)

Mpp = tuple[float, float]


def px_to_um_matrix(downsample: float, mpp: Mpp) -> np.ndarray:
    """3x3 matrix: level pixel coordinates (OpenCV convention) -> slice µm (C1)."""
    sx, sy = downsample * mpp[0], downsample * mpp[1]
    return np.array([[sx, 0.0, 0.5 * sx], [0.0, sy, 0.5 * sy], [0.0, 0.0, 1.0]])


def um_to_px_matrix(downsample: float, mpp: Mpp) -> np.ndarray:
    """3x3 matrix: slice µm -> level pixel coordinates (inverse of `px_to_um_matrix`)."""
    sx, sy = downsample * mpp[0], downsample * mpp[1]
    return np.array([[1 / sx, 0.0, -0.5], [0.0, 1 / sy, -0.5], [0.0, 0.0, 1.0]])


def _apply3(m: np.ndarray, points: np.ndarray) -> np.ndarray:
    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    return pts @ m[:2, :2].T + m[:2, 2]


def px_to_um(points: np.ndarray, downsample: float, mpp: Mpp) -> np.ndarray:
    """(N, 2) level pixel coordinates -> (N, 2) µm."""
    return _apply3(px_to_um_matrix(downsample, mpp), points)


def um_to_px(points: np.ndarray, downsample: float, mpp: Mpp) -> np.ndarray:
    """(N, 2) µm -> (N, 2) level pixel coordinates."""
    return _apply3(um_to_px_matrix(downsample, mpp), points)


def _validate(m: np.ndarray, family: Family) -> None:
    if family not in ("rigid", "similarity"):
        raise ValueError(f"unknown transform family {family!r}")
    if not np.all(np.isfinite(m)):
        raise ValueError("transform matrix contains non-finite values")
    a, b = m[0, 0], m[1, 0]
    scale = math.hypot(a, b)
    tol = _RTOL * max(1.0, scale)
    # The structure [[a, -b], [b, a]] has determinant a^2 + b^2 > 0, so reflections (C3) fail here.
    if scale <= 0 or abs(m[1, 1] - a) > tol or abs(m[0, 1] + b) > tol:
        raise ValueError(f"not a similarity (rotation + uniform scale) matrix: {m[:, :2].tolist()}")
    if family == "rigid" and abs(scale - 1.0) > tol:
        raise ValueError(f"rigid transform must have scale 1, got {scale}")


class Transform2D:
    """Immutable similarity transform, slice frame -> reference frame, in µm."""

    __slots__ = ("_m", "family")

    def __init__(self, matrix: np.ndarray, family: Family = "similarity") -> None:
        m = np.array(matrix, dtype=float)
        if m.shape == (3, 3):
            if not np.allclose(m[2], [0.0, 0.0, 1.0]):
                raise ValueError("3x3 matrix must have last row [0, 0, 1]")
            m = m[:2]
        if m.shape != (2, 3):
            raise ValueError(f"expected a 2x3 or 3x3 matrix, got shape {m.shape}")
        _validate(m, family)
        m.flags.writeable = False
        self._m = m
        self.family: Family = family

    # --- constructors -------------------------------------------------------------------------
    @classmethod
    def identity(cls, family: Family = "similarity") -> "Transform2D":
        return cls(np.eye(2, 3), family)

    @classmethod
    def similarity(
        cls, angle_rad: float, scale: float = 1.0, tx_um: float = 0.0, ty_um: float = 0.0
    ) -> "Transform2D":
        c, s = scale * math.cos(angle_rad), scale * math.sin(angle_rad)
        return cls(np.array([[c, -s, tx_um], [s, c, ty_um]]), "similarity")

    @classmethod
    def rigid(cls, angle_rad: float, tx_um: float = 0.0, ty_um: float = 0.0) -> "Transform2D":
        c, s = math.cos(angle_rad), math.sin(angle_rad)
        return cls(np.array([[c, -s, tx_um], [s, c, ty_um]]), "rigid")

    @classmethod
    def from_matrix(cls, matrix: np.ndarray, family: Family = "similarity") -> "Transform2D":
        """Validated construction from a 2x3 or 3x3 matrix in µm."""
        return cls(matrix, family)

    @classmethod
    def from_pixels(
        cls,
        matrix_px: np.ndarray,
        src_downsample: float,
        dst_downsample: float,
        mpp: Mpp,
        family: Family = "similarity",
    ) -> "Transform2D":
        """Inverse of `to_pixels`: a pixel-space map (e.g. from OpenCV) expressed in µm.

        `matrix_px` maps source-level pixels (moving slice) to destination-level pixels
        (fixed slice); the result maps the moving slice's µm frame to the fixed slice's.
        """
        m = np.array(matrix_px, dtype=float)
        if m.shape == (2, 3):
            m = np.vstack([m, [0.0, 0.0, 1.0]])
        um = px_to_um_matrix(dst_downsample, mpp) @ m @ um_to_px_matrix(src_downsample, mpp)
        return cls(um, family)

    # --- properties ---------------------------------------------------------------------------
    @property
    def matrix(self) -> np.ndarray:
        """2x3 matrix in µm (a copy)."""
        return self._m.copy()

    @property
    def matrix3(self) -> np.ndarray:
        """3x3 homogeneous matrix in µm."""
        return np.vstack([self._m, [0.0, 0.0, 1.0]])

    @property
    def scale(self) -> float:
        return math.hypot(self._m[0, 0], self._m[1, 0])

    @property
    def angle_rad(self) -> float:
        return math.atan2(self._m[1, 0], self._m[0, 0])

    @property
    def angle_deg(self) -> float:
        return math.degrees(self.angle_rad)

    @property
    def translation_um(self) -> tuple[float, float]:
        return float(self._m[0, 2]), float(self._m[1, 2])

    # --- operations ---------------------------------------------------------------------------
    def apply(self, points_um: np.ndarray) -> np.ndarray:
        """(N, 2) points in the slice frame (µm) -> (N, 2) points in the reference frame."""
        return _apply3(self._m, points_um)

    def inverse(self) -> "Transform2D":
        return Transform2D(np.linalg.inv(self.matrix3), self.family)

    def compose(self, other: "Transform2D") -> "Transform2D":
        """self ∘ other: apply `other` first, then `self`."""
        family: Family = "rigid" if self.family == other.family == "rigid" else "similarity"
        return Transform2D(self.matrix3 @ other.matrix3, family)

    def to_pixels(self, src_downsample: float, dst_downsample: float, mpp: Mpp) -> np.ndarray:
        """3x3 map: source-level pixels of this slice -> destination-level reference pixels.

        Suitable for `cv2.warpAffine` (use the top two rows, with `WARP_INVERSE_MAP` for the
        inverse direction). Not necessarily a similarity if mpp_x != mpp_y.
        """
        return (
            um_to_px_matrix(dst_downsample, mpp)
            @ self.matrix3
            @ px_to_um_matrix(src_downsample, mpp)
        )

    def allclose(self, other: "Transform2D", atol_um: float = 1e-6, rtol: float = 1e-9) -> bool:
        return np.allclose(self._m, other._m, rtol=rtol, atol=atol_um)

    # --- persistence --------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"schema": SCHEMA, "family": self.family, "matrix_um": self._m.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "Transform2D":
        if d.get("schema") != SCHEMA:
            raise ValueError(f"unsupported transform schema {d.get('schema')!r}")
        return cls(np.array(d["matrix_um"], dtype=float), d["family"])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> "Transform2D":
        return cls.from_dict(json.loads(path.read_text()))

    def __repr__(self) -> str:
        tx, ty = self.translation_um
        return (
            f"Transform2D({self.family}, angle={self.angle_deg:.4f} deg, scale={self.scale:.6f}, "
            f"t=({tx:.2f}, {ty:.2f}) um)"
        )
