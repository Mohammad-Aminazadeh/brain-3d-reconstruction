"""Registration image (contract C4): tissue bright, background exactly 0."""

import cv2
import numpy as np

CLAHE_CLIP = 2.0
CLAHE_TILES = (8, 8)


def make_reg_image(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """uint8 image: 255 minus luminance, CLAHE, 0 outside the mask.

    The background is set to 0 before CLAHE as well as after, so glass and debris cannot
    change the contrast inside the tissue (found by the debris-invariance test in M3-07).
    """
    if rgb.shape[:2] != mask.shape or rgb.ndim != 3:
        raise ValueError(f"rgb {rgb.shape} and mask {mask.shape} do not match")
    inv = 255 - cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    inv[mask == 0] = 0
    img = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=CLAHE_TILES).apply(inv)
    img[mask == 0] = 0
    return img
