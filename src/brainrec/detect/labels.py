"""Labelled cells for measuring detection and classification accuracy (M7-02).

Labelling happens in QuPath on the original slides: square crops are handed over as locked
rectangle annotations, the labeller places one point per nucleus with its appearance class
(decision 0006) and outlines a few nuclei for their size, and exports the objects as GeoJSON.

Coordinates: QuPath uses continuous level-0 pixel coordinates with the origin at the corner of
pixel (0, 0); multiplied by the level-0 pixel size they are the slice frame of contract C1 (µm).
"""

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

SCHEMA = "brainrec.labelcrops/1"
# first labelling round (owner, 2026-11-08, decision 0006); may change after M7-18 and Q9
CLASSES = (
    "Neuron",
    "Astrocyte-like",
    "Oligodendrocyte-like",
    "Microglia-like",
    "Vessel-wall",
    "Other",
)
CROP_PREFIX = "crop "  # names of the crop rectangles; never read as labels


@dataclass(frozen=True)
class Crop:
    """A square region of one slice to be labelled completely (µm, slice frame C1)."""

    id: int
    slice_id: int
    source: str  # file name of the slide
    x0_um: float
    y0_um: float
    size_um: float
    split: str  # "train" or "test"

    def inside(self, x_um: np.ndarray, y_um: np.ndarray, margin_um: float = 0.0) -> np.ndarray:
        """True for points at least `margin_um` inside the crop (edges count as inside)."""
        lo_x, lo_y = self.x0_um + margin_um, self.y0_um + margin_um
        hi_x, hi_y = self.x0_um + self.size_um - margin_um, self.y0_um + self.size_um - margin_um
        return (x_um >= lo_x) & (x_um <= hi_x) & (y_um >= lo_y) & (y_um <= hi_y)


@dataclass
class Labels:
    """Labelled nuclei: centre (µm, slice frame), class name, and radius (µm, equivalent-area
    circle) for outlined nuclei (NaN for points). `crop` is the crop id, -1 for extra examples
    placed outside every crop (usable for classification only)."""

    slice_id: np.ndarray
    crop: np.ndarray
    x_um: np.ndarray
    y_um: np.ndarray
    cls: np.ndarray
    radius_um: np.ndarray

    def __len__(self) -> int:
        return len(self.x_um)

    @classmethod
    def concat(cls, parts: list["Labels"]) -> "Labels":
        names = ("slice_id", "crop", "x_um", "y_um", "cls", "radius_um")
        if not parts:
            return cls(*(np.empty(0) for _ in names))
        return cls(*(np.concatenate([getattr(p, n) for p in parts]) for n in names))


# --- choosing crops ---------------------------------------------------------------------------


def candidate_centres(
    mask: np.ndarray, um_per_px: float, min_depth_um: float, seed: int
) -> np.ndarray:
    """Possible crop centres (µm, slice frame C1) at least `min_depth_um` inside the tissue of
    `mask` (uint8, 255 = tissue, at `um_per_px`), in a random order fixed by `seed`. (n, 2)."""
    depth = cv2.distanceTransform((mask > 0).astype(np.uint8), cv2.DIST_L2, 5) * um_per_px
    ys, xs = np.nonzero(depth >= min_depth_um)
    order = np.random.default_rng(seed).permutation(len(xs))
    return (np.stack([xs[order], ys[order]], axis=1) + 0.5) * um_per_px


def far_enough(centre: np.ndarray, chosen: list[np.ndarray], min_distance_um: float) -> bool:
    return all(float(np.linalg.norm(centre - c)) >= min_distance_um for c in chosen)


def assign_split(slice_ids: list[int], n_test: int, seed: int) -> list[str]:
    """ "train" or "test" per crop: `n_test` test crops, at most one per slice, from slices spread
    evenly over the block (by id). Raises ValueError if there are fewer slices than `n_test`."""
    slices = sorted(set(slice_ids))
    if n_test > len(slices):
        raise ValueError(f"{n_test} test crops need at least {n_test} slices, got {len(slices)}")
    rng = np.random.default_rng(seed)
    picks = np.unique(np.round(np.linspace(0, len(slices) - 1, n_test)).astype(int))
    split = ["train"] * len(slice_ids)
    for i in picks:
        members = [k for k, s in enumerate(slice_ids) if s == slices[i]]
        split[int(rng.choice(members))] = "test"
    return split


def crops_geojson(crops: list[Crop], mpp_um: float) -> dict[str, Any]:
    """The crops as locked QuPath rectangle annotations (level-0 pixels), named "crop NN".
    The split is not shown, so training and test crops are labelled alike."""
    features = []
    for c in crops:
        x0, y0 = c.x0_um / mpp_um, c.y0_um / mpp_um
        x1, y1 = (c.x0_um + c.size_um) / mpp_um, (c.y0_um + c.size_um) / mpp_um
        ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    "objectType": "annotation",
                    "name": f"{CROP_PREFIX}{c.id:02d}",
                    "isLocked": True,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def points_geojson(
    labels: Labels, crops: list[Crop], mpp_um: float, source: str = "draft"
) -> dict[str, Any]:
    """Labels of one slide as QuPath objects: its crop squares plus one point set (MultiPoint
    annotation) per crop and class, marked with `source` (for example "draft"), so they can be
    opened in QuPath, corrected and exported again. Labels must all belong to one slide."""
    slices = set(labels.slice_id.tolist())
    if len(slices) > 1:
        raise ValueError(f"labels of several slices {sorted(slices)}; write one slide at a time")
    own = [c for c in crops if c.slice_id in slices] if slices else []
    features = crops_geojson(own, mpp_um)["features"]
    for crop_id in sorted(set(labels.crop.tolist())):
        for cls in sorted(set(labels.cls[labels.crop == crop_id].tolist())):
            sel = (labels.crop == crop_id) & (labels.cls == cls)
            pts = np.stack([labels.x_um[sel], labels.y_um[sel]], axis=1) / mpp_um
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "MultiPoint", "coordinates": pts.tolist()},
                    "properties": {
                        "objectType": "annotation",
                        "name": f"{source} {cls}",
                        "classification": {"name": cls},
                        "source": source,
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


def save_crops(path: Path, crops: list[Crop], classes: tuple[str, ...], mpp_um: float) -> None:
    data = {
        "schema": SCHEMA,
        "mpp_um": mpp_um,
        "classes": list(classes),
        "crops": [asdict(c) for c in crops],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_crops(path: Path) -> tuple[list[Crop], tuple[str, ...], float]:
    """Crops, class names and level-0 pixel size (µm) written by `save_crops`."""
    data = json.loads(path.read_text())
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: unsupported schema {data.get('schema')!r}")
    return [Crop(**c) for c in data["crops"]], tuple(data["classes"]), float(data["mpp_um"])


# --- reading QuPath exports ------------------------------------------------------------------


def _polygon_centre_area(ring: np.ndarray) -> tuple[np.ndarray, float]:
    x, y = ring[:, 0], ring[:, 1]
    cross = x * np.roll(y, -1) - np.roll(x, -1) * y
    area = cross.sum() / 2
    if abs(area) < 1e-12:
        return ring.mean(axis=0), 0.0
    cx = ((x + np.roll(x, -1)) * cross).sum() / (6 * area)
    cy = ((y + np.roll(y, -1)) * cross).sum() / (6 * area)
    return np.array([cx, cy]), abs(float(area))


def _class_name(props: dict[str, Any]) -> str | None:
    c = props.get("classification")
    if isinstance(c, dict):
        return c.get("name")
    return c if isinstance(c, str) else None


def read_qupath(
    data: dict[str, Any] | list[Any],
    slice_id: int,
    mpp_um: float,
    crops: list[Crop],
    classes: tuple[str, ...] = CLASSES,
) -> Labels:
    """Labels of one slide from a QuPath GeoJSON export (FeatureCollection or feature list).

    Points and multipoints give one label per point; polygons (outlined nuclei) give their
    centroid and the radius of the circle of equal area. Crop rectangles are skipped. Class
    names are matched to `classes` ignoring case and surrounding spaces. Raises ValueError
    naming unknown classes, labels without a class, or unsupported geometries, so that a
    labelling slip is fixed instead of silently dropped.
    """
    features = data["features"] if isinstance(data, dict) else data
    known = {c.lower(): c for c in classes}
    xs: list[float] = []
    ys: list[float] = []
    names: list[str] = []
    radii: list[float] = []
    unknown: Counter[str] = Counter()
    unclassified = 0
    for f in features:
        props = f.get("properties") or {}
        if str(props.get("name", "")).startswith(CROP_PREFIX):
            continue
        geom = f["geometry"]
        kind = geom["type"]
        if kind == "Point":
            pts, rad = [geom["coordinates"]], [np.nan]
        elif kind == "MultiPoint":
            pts, rad = geom["coordinates"], [np.nan] * len(geom["coordinates"])
        elif kind == "Polygon":
            centre, area = _polygon_centre_area(np.asarray(geom["coordinates"][0], float))
            pts, rad = [centre.tolist()], [float(np.sqrt(area / np.pi)) * mpp_um]
        else:
            raise ValueError(f"unsupported geometry {kind!r} in the labels of slice {slice_id}")
        name = _class_name(props)
        if name is None:
            unclassified += len(pts)
            continue
        cls = known.get(name.strip().lower())
        if cls is None:
            unknown[name] += len(pts)
            continue
        for (px, py), r in zip(pts, rad, strict=True):
            xs.append(float(px) * mpp_um)
            ys.append(float(py) * mpp_um)
            names.append(cls)
            radii.append(r)
    problems = []
    if unclassified:
        problems.append(f"{unclassified} labels have no class")
    if unknown:
        listed = ", ".join(f"{n!r} ({k})" for n, k in sorted(unknown.items()))
        problems.append(f"unknown classes {listed}; expected one of {', '.join(classes)}")
    if problems:
        raise ValueError(f"labels of slice {slice_id}: " + "; ".join(problems))

    x, y = np.array(xs, float), np.array(ys, float)
    crop = np.full(len(x), -1, int)
    for c in crops:
        if c.slice_id == slice_id:
            crop[(crop == -1) & c.inside(x, y)] = c.id
    return Labels(
        np.full(len(x), slice_id, int), crop, x, y, np.array(names, dtype=object),
        np.array(radii, float),
    )  # fmt: skip


def load_labels(
    annotations_dir: Path, crops: list[Crop], classes: tuple[str, ...], mpp_um: float
) -> Labels:
    """All label files `<slide file name>.geojson` in `annotations_dir`, matched to the crops'
    slides by file name. Raises FileNotFoundError naming slides of crops without a label file."""
    by_source = {c.source: c.slice_id for c in crops}
    missing = sorted(s for s in by_source if not (annotations_dir / f"{s}.geojson").exists())
    if missing:
        raise FileNotFoundError(f"no label file for {', '.join(missing)} in {annotations_dir}")
    parts = [
        read_qupath(
            json.loads((annotations_dir / f"{source}.geojson").read_text()),
            slice_id,
            mpp_um,
            crops,
            classes,
        )
        for source, slice_id in sorted(by_source.items(), key=lambda kv: kv[1])
    ]
    return Labels.concat(parts)
