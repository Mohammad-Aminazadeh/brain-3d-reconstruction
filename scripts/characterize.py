"""M1: read-only characterisation of NDPI slides (levels, read speed, compressibility).

Usage: python scripts/characterize.py SLIDE.ndpi [SLIDE2.ndpi ...]
Opens files read-only; writes nothing except stdout.
"""

import sys
import time
from pathlib import Path

import cv2
import numpy as np
import openslide

CROP = 2048  # crop edge in pixels for read-time and compression tests
MAX_FULL_LEVEL_PX = 60_000_000  # read whole level only if smaller than this


def to_rgb(img) -> np.ndarray:
    return np.asarray(img.convert("RGB"))


def find_tissue_center(slide: openslide.OpenSlide) -> tuple[int, int]:
    """Level-0 (x, y) of the tissue centroid, from the coarsest level."""
    lvl = slide.level_count - 1
    w, h = slide.level_dimensions[lvl]
    arr = to_rgb(slide.read_region((0, 0), lvl, (w, h)))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ys, xs = np.nonzero(mask)
    ds = slide.level_downsamples[lvl]
    return int(xs.mean() * ds), int(ys.mean() * ds)


def jpeg_bytes(arr: np.ndarray, quality: int) -> int:
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return len(buf)


def characterize(path: Path) -> None:
    print(f"\n=== {path.name}  ({path.stat().st_size / 1e9:.2f} GB)")
    t0 = time.perf_counter()
    slide = openslide.OpenSlide(str(path))
    print(f"open: {time.perf_counter() - t0:.2f}s  levels={slide.level_count}")
    mpp_x = float(slide.properties.get(openslide.PROPERTY_NAME_MPP_X, "nan"))
    print(f"mpp level0: {mpp_x:.4f} um/px   vendor={slide.properties.get('openslide.vendor')}")

    t0 = time.perf_counter()
    cx, cy = find_tissue_center(slide)
    print(f"tissue centre (level0): ({cx}, {cy})  [{time.perf_counter() - t0:.2f}s]")

    print(
        f"\n{'lvl':>3} {'downs':>7} {'size':>16} {'um/px':>7} {'Mpx':>9} "
        f"{'crop s':>7} {'full s':>7} {'jpeg90 B/px':>11} {'est GB/slice@q90':>16}"
    )
    for lvl in range(slide.level_count):
        w, h = slide.level_dimensions[lvl]
        ds = slide.level_downsamples[lvl]
        mpx = w * h / 1e6
        cw, ch = min(CROP, w), min(CROP, h)
        x0 = max(0, int(cx / ds) - cw // 2)
        y0 = max(0, int(cy / ds) - ch // 2)
        t0 = time.perf_counter()
        crop = to_rgb(slide.read_region((int(x0 * ds), int(y0 * ds)), lvl, (cw, ch)))
        t_crop = time.perf_counter() - t0
        bpp = jpeg_bytes(crop, 90) / (cw * ch)
        t_full = float("nan")
        if w * h <= MAX_FULL_LEVEL_PX:
            t0 = time.perf_counter()
            to_rgb(slide.read_region((0, 0), lvl, (w, h)))
            t_full = time.perf_counter() - t0
        # tissue-centre crop is denser than average; treat estimate as an upper bound
        est_gb = bpp * w * h / 1e9
        print(
            f"{lvl:>3} {ds:>7.1f} {w:>7}x{h:<8} {mpp_x * ds:>7.3f} {mpx:>9.1f} "
            f"{t_crop:>7.2f} {t_full:>7.2f} {bpp:>11.3f} {est_gb:>16.2f}"
        )
    slide.close()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        characterize(Path(p))


if __name__ == "__main__":
    main()
