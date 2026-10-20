"""Data passed between registration steps (contracts C2, C4, C12)."""

import json
from dataclasses import dataclass
from typing import Any, Literal

import cv2
import numpy as np

from brainrec.registration.transform import Mpp, Transform2D
from brainrec.store import WorkDir

REGIMAGE_SCHEMA = "brainrec.regimage/1"

Status = Literal["features", "boundary", "failed"]


@dataclass(frozen=True)
class RegImage:
    """Registration image of one slice: uint8 image (tissue bright, 0 outside the mask) and mask.

    `downsample` is the pyramid level of the image; `mpp_um` the level-0 pixel size.
    """

    slice_id: int
    image: np.ndarray
    mask: np.ndarray
    downsample: float
    mpp_um: Mpp

    @classmethod
    def load(cls, work: WorkDir, slice_id: int) -> "RegImage":
        """Read the registration image, mask and metadata written by preprocessing (C4)."""
        meta = json.loads(work.reg_meta(slice_id).read_text())
        if meta.get("schema") != REGIMAGE_SCHEMA:
            raise ValueError(f"unsupported registration image schema {meta.get('schema')!r}")
        image = cv2.imread(str(work.reg_image(slice_id)), cv2.IMREAD_GRAYSCALE)
        mask = cv2.imread(str(work.mask(slice_id)), cv2.IMREAD_GRAYSCALE)
        if image is None or mask is None:
            raise FileNotFoundError(f"registration image or mask of slice {slice_id} missing")
        if image.shape != mask.shape or image.shape != (meta["height"], meta["width"]):
            raise ValueError(f"slice {slice_id}: image, mask and metadata sizes differ")
        mpp = (float(meta["mpp_um"][0]), float(meta["mpp_um"][1]))
        return cls(slice_id, image, mask, float(meta["downsample"]), mpp)

    @property
    def um_per_px(self) -> float:
        return self.downsample * self.mpp_um[0]

    def coarse(self, factor: int = 2) -> "RegImage":
        """Area-downsampled copy (pixel centres stay consistent with C1)."""
        h, w = self.image.shape[0] // factor, self.image.shape[1] // factor
        img = cv2.resize(
            self.image[: h * factor, : w * factor], (w, h), interpolation=cv2.INTER_AREA
        )
        msk = cv2.resize(
            self.mask[: h * factor, : w * factor], (w, h), interpolation=cv2.INTER_AREA
        )
        msk = np.where(msk >= 128, 255, 0).astype(np.uint8)
        img[msk == 0] = 0
        return RegImage(self.slice_id, img, msk, self.downsample * factor, self.mpp_um)


@dataclass(frozen=True)
class PairResult:
    """Result of registering `moving` onto `fixed`; `transform` maps moving -> fixed (µm)."""

    fixed: int
    moving: int
    transform: Transform2D
    status: Status
    n_matches: int
    n_inliers: int
    rms_um: float | None  # residual of the feature fit on its inliers; None without a fit
    mask_iou: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "brainrec.pair/1",
            "fixed": self.fixed,
            "moving": self.moving,
            "status": self.status,
            "n_matches": self.n_matches,
            "n_inliers": self.n_inliers,
            "rms_um": self.rms_um,
            "mask_iou": self.mask_iou,
            "transform": self.transform.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PairResult":
        if d.get("schema") != "brainrec.pair/1":
            raise ValueError(f"unsupported pair schema {d.get('schema')!r}")
        return cls(
            d["fixed"], d["moving"], Transform2D.from_dict(d["transform"]), d["status"],
            d["n_matches"], d["n_inliers"], d["rms_um"], d["mask_iou"],
        )  # fmt: skip
