"""Cell detection, measurement and classification per slice (M7, decisions 0005 and 0006)."""

from brainrec.detect.evaluate import Detections, evaluate, match
from brainrec.detect.labels import CLASSES, Crop, Labels, load_crops, load_labels, read_qupath

__all__ = [
    "CLASSES",
    "Crop",
    "Detections",
    "Labels",
    "evaluate",
    "load_crops",
    "load_labels",
    "match",
    "read_qupath",
]
