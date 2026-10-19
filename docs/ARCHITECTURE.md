# Architecture

Status: approved design. No implementation exists yet. Interface signatures below are the contract; changing them requires an explicit discussion (see CLAUDE.md).

## 1. Overview

A **staged batch pipeline plus a thin tile server and web viewer**, delivered as a single Python package (`brainrec`) with a CLI. Stages communicate only through files on disk, which makes them resumable and independently testable.

Principles:

1. **Transforms are separate from pixels.** Registration runs on low-resolution images and stores small transform files. Full-resolution pixels are only touched when building the volume and when serving tiles.
2. **No full slice is ever loaded at full resolution.** Work happens on pyramid levels or tiles.
3. **The volume is a multi-resolution chunked store (OME-Zarr)**; the viewer requests only the level and region it needs.
4. **Source files are read-only.** All outputs go to a separate work directory.
5. **Two processes only:** the offline pipeline (CLI) and the tile server (which also serves viewer files).

## 2. Component responsibilities

| Component | Responsibility | Inputs | Outputs | Depends on |
|---|---|---|---|---|
| `slides` | Open NDPI, expose metadata, return regions at a pyramid level | NDPI path | Metadata, numpy arrays | OpenSlide / tifffile |
| `catalog` | Discover files, parse `N_H`, validate completeness and order, hold project config | Data folder, config | Ordered slice list | `slides` |
| `preprocess` | Create the low-res registration image and tissue mask per slice | `SlideReader` | `reg/{id}.tif`, `mask/{id}.png` | `slides`, `store` |
| `registration` | Estimate a transform per consecutive pair; chain; correct drift | Reg images, masks | `transforms/{id}.json` (+ displacement field if non-rigid) | SimpleITK, `store` |
| `qc` | Quality metrics, overlays, flagging, manual overrides | Reg images, transforms | `qc/report.json`, overlay images | `registration` |
| `volume` | Resample slices through transforms tile by tile into the pyramid | Slides, transforms, voxel size | `volume.zarr` | `slides`, `registration`, zarr |
| `serve` | Serve volume chunks over HTTP (later: on-demand native-resolution tiles) | `volume.zarr` | HTTP responses | FastAPI, `volume` |
| `viewer` | 3D and orthogonal views, zoom, level-of-detail streaming | HTTP | Rendered view | Neuroglancer (MVP) |
| `pipeline` | CLI and orchestrator; per-slice status; resume | Config | Manifest | all stages |
| `store` | File layout, formats and manifest shared by all stages | n/a | n/a | none |

## 3. Data flow

```mermaid
flowchart LR
    NDPI[("140 NDPI files<br/>(read-only)")] --> CAT[catalog]
    CAT --> SL[slides reader]
    SL --> PRE[preprocess]
    PRE -->|reg images + masks| REG[registration]
    REG -->|transforms| QC[qc]
    QC -->|flagged pairs| OVR[manual overrides]
    OVR --> REG
    REG -->|approved transforms| VOL[volume builder]
    SL -->|tile reads| VOL
    VOL --> ZARR[("volume.zarr<br/>multi-res pyramid")]
    ZARR --> SRV[tile server]
    SRV --> WEB[web viewer]
    SL -. post-MVP: native-res tiles .-> SRV
    REG -. post-MVP .-> SRV
    ORCH[pipeline CLI] -.controls.-> PRE
    ORCH -.-> REG
    ORCH -.-> VOL
```

## 4. Component dependencies

```mermaid
flowchart TD
    subgraph Runtime
      pipeline --> catalog
      pipeline --> preprocess
      pipeline --> registration
      pipeline --> volume
      pipeline --> qc
      catalog --> slides
      preprocess --> slides
      volume --> slides
      volume --> registration
      qc --> registration
      serve --> volume
      viewer -. HTTP .-> serve
      slides --> store
      preprocess --> store
      registration --> store
      volume --> store
    end
    subgraph Development
      pytest
      ruff
      mypy
      synthetic[synthetic data generator]
      precommit[pre-commit]
    end
    pytest -.tests.-> slides
    pytest -.tests.-> registration
    pytest -.tests.-> volume
    synthetic -.-> pytest
```

Rules: dependencies point downward only (no cycles); stages never import each other's internals, only the interfaces below and `store`.

## 5. Interface definitions

Python signatures (type-annotated, shown as pseudocode). These are the contracts; they are not implemented yet.

```
# slides
class SlideReader:
    @classmethod open(path: Path) -> SlideReader
    levels: list[LevelInfo]            # width, height, downsample
    mpp: tuple[float, float]           # microns per pixel at level 0
    read_region(level: int, x: int, y: int, w: int, h: int) -> ndarray  # RGB uint8

# catalog
class Catalog:
    @classmethod scan(data_dir: Path, config: ProjectConfig) -> Catalog
    slices: list[SliceRef]             # id (int N), path, in order
    voxel_size: tuple[float, float, float]  # um, Z from config

# preprocess
make_reg_image(slice: SliceRef, out_dir: Path) -> Path
make_mask(reg_image: Path, out_dir: Path) -> Path

# registration
register_pair(fixed: Path, moving: Path, fixed_mask: Path | None, moving_mask: Path | None) -> Transform
chain(transforms: list[Transform]) -> list[Transform]       # slice-to-reference
correct_drift(chained: list[Transform]) -> list[Transform]
Transform.save(path), Transform.load(path), Transform.apply_to_point(...)

# qc
score_pair(fixed: Path, moving: Path, t: Transform) -> PairScore
render_overlay(fixed, moving, t, out: Path) -> Path
apply_override(slice_id: int, t: Transform, store: Store) -> None

# volume
build_volume(catalog: Catalog, transforms: list[Transform], out: Path, levels: int) -> Path

# serve (HTTP)
GET /volume/{path}                 # raw Zarr chunks, CORS enabled
GET /tile/{z}/{level}/{x}/{y}      # post-MVP, native-resolution tile

# pipeline (CLI)
brainrec run [--stage S] [--slices A-B]
brainrec status
brainrec qc
```

## 6. Technology decisions

| Concern | Choice | Rationale | Trade-off |
|---|---|---|---|
| Language | Python 3.12+ | Best ecosystem for whole-slide images, registration and Zarr | Slower than C++; heavy loops run in native libraries |
| NDPI reading | OpenSlide (`openslide-python`), tifffile as fallback | Lazy tile access to NDPI | Needs native library via Homebrew |
| Storage | OME-Zarr (`zarr`, `ome-zarr-py`) | Chunked, multi-resolution, viewer-friendly, resumable | Many small files; chunk size needs tuning |
| Image ops | numpy, scikit-image, OpenCV | Standard and fast | |
| Registration | SimpleITK (elastix later if needed) | Mature multi-resolution rigid and B-spline, serialisable transforms | Steeper learning curve |
| Parallelism | `concurrent.futures`; Dask only if needed | Simplicity | |
| Server | FastAPI + uvicorn | Lightweight, testable | |
| Viewer | Neuroglancer (MVP) | Solves 3D streaming and orthogonal views, saving weeks | Less control over UX; replaceable, isolated to `viewer/` and `serve/` |
| Config | YAML + pydantic | Validated and versionable | |
| Packaging | `pyproject.toml` (uv or pip) | Reproducible | |
| Testing | pytest, synthetic slice generator | Independent tests without the real data | |

## 7. Project structure

```
Brain 3D Reconstruction/
├── pyproject.toml
├── README.md, CLAUDE.md
├── config/project.example.yaml
├── src/brainrec/
│   ├── slides/  catalog/  preprocess/  registration/
│   ├── qc/  volume/  serve/  store/  pipeline/
├── viewer/
├── tests/ (unit/, integration/, synthetic/)
├── scripts/
└── docs/ (REQUIREMENTS.md, ARCHITECTURE.md, decisions/)

Work directory (outside repo): /Volumes/My Passport/3D Brain Reconstruction/
    reg/ mask/ transforms/ qc/ volume.zarr/ manifest.json
```

## 8. Risks and limitations

- **Storage:** 467 GB free vs. about 530 GB of sources. Each slice is 139776 x 102144 px at 220 nm/px (about 14.3 Gpx, about 31 x 22 mm), i.e. about 43 GB raw RGB per slice and about 6 TB for 140. Native-resolution precomputation is infeasible; it must be served on demand (post-MVP) and may be slow from the external drive. Estimates for a precomputed floor: 4x downsample (0.88 um/px) is about 2.7 GB raw per slice (about 375 GB raw, roughly tens of GB with JPEG-type compression); 2x (0.44 um/px) is about 10.7 GB raw per slice (about 1.5 TB raw, possibly around 100 GB compressed, to be measured). The floor is chosen after measuring real compression.
- **Drift:** errors accumulate across 140 chained pairs; mitigated by drift correction and QC; unsolved in general.
- **Non-rigid distortion:** can deform real anatomy; kept behind a flag.
- **Damaged sections** (tears, folds) will need manual overrides.
- **Anisotropy:** Z spacing is much larger than in-plane pixel size, so side views look like stacked slices.
- **Viewer:** Neuroglancer needs a compatible Zarr layout and CORS; its UX may not suit.
- **I/O:** the external drive is the speed limit; limit passes over each source slice.
- **Environment:** OpenSlide needs a native install; nothing is installed until approved.
