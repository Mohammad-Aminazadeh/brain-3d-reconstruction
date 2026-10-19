"""NDPI slide access. Only level-local coordinates are exposed; callers never see level-0 offsets."""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import openslide


@dataclass(frozen=True)
class LevelInfo:
    index: int
    downsample: float
    width: int
    height: int


class SlideReader:
    """Read-only view of one whole-slide image."""

    def __init__(self, slide: openslide.OpenSlide) -> None:
        self._slide = slide
        self.levels: list[LevelInfo] = [
            LevelInfo(i, float(slide.level_downsamples[i]), *slide.level_dimensions[i])
            for i in range(slide.level_count)
        ]

    @classmethod
    def open(cls, path: Path) -> "SlideReader":
        return cls(openslide.OpenSlide(str(path)))

    @property
    def mpp(self) -> tuple[float, float]:
        """Microns per pixel (x, y) at level 0."""
        props = self._slide.properties
        try:
            return (
                float(props[openslide.PROPERTY_NAME_MPP_X]),
                float(props[openslide.PROPERTY_NAME_MPP_Y]),
            )
        except KeyError as exc:
            raise ValueError("slide has no pixel-size metadata") from exc

    def level_for_downsample(self, downsample: float) -> int:
        """Index of the level whose downsample factor equals `downsample` (levels vary per file)."""
        for lv in self.levels:
            if abs(lv.downsample - downsample) < 1e-6 * downsample:
                return lv.index
        raise ValueError(f"no level with downsample {downsample}")

    def read_region(self, level: int, x: int, y: int, w: int, h: int) -> np.ndarray:
        """RGB uint8 array (h, w, 3) at `level`; (x, y) are in that level's pixels."""
        ds = self.levels[level].downsample
        img = self._slide.read_region((round(x * ds), round(y * ds)), level, (w, h))
        rgba = np.asarray(img)
        # Transparent pixels (outside the scanned area) become white background.
        return np.where(rgba[..., 3:4] == 0, 255, rgba[..., :3]).astype(np.uint8)

    def close(self) -> None:
        self._slide.close()
