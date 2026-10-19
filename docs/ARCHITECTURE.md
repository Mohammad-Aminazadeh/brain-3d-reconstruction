# Architecture

Status: approved design; `slides`, `catalog`, `store`, `volume` (unregistered) and `serve` implemented in M2. Updated 2026-10-19 for tissue-only processing (F12) and feature-based registration (F13), see `docs/decisions/0003-tissue-only-feature-based-registration.md`. Interface signatures below are the contract; changing them requires an explicit discussion (see CLAUDE.md).

## 1. Overview

A **staged batch pipeline plus a thin tile server and web viewer**, delivered as a single Python package (`brainrec`) with a CLI. Stages communicate only through files on disk, which makes them resumable and independently testable.

Principles:

1. **Transforms are separate from pixels.** Registration runs on low-resolution images and stores small transform files. Full-resolution pixels are only touched when building the volume and when serving tiles.
2. **No full slice is ever loaded at full resolution.** Work happens on pyramid levels or tiles.
3. **The volume is a multi-resolution chunked store (OME-Zarr)**; the viewer requests only the level and region it needs.
4. **Source files are read-only.** All outputs go to a separate work directory.
5. **Two processes only:** the offline pipeline (CLI) and the tile server (which also serves viewer files).
6. **Only tissue matters (F12).** Each slice's tissue mask defines what is processed: registration detects, matches and scores features inside the mask only, and the volume stores only tissue (outside the mask = background, stored as pure white; volume limited to the tissue extent). The viewer shows the background transparent.
7. **Registration is feature-based (F13).** The clearly visible tissue boundaries give the initial alignment; keypoint features inside the tissue, matched between neighbouring slices and fitted robustly (RANSAC), give the transform.

## 2. Component responsibilities

| Component | Responsibility | Inputs | Outputs | Depends on |
|---|---|---|---|---|
| `slides` | Open NDPI, expose metadata, return regions at a pyramid level | NDPI path | Metadata, numpy arrays | OpenSlide / tifffile |
| `catalog` | Discover files, parse `N_H`, validate completeness and order, hold project config | Data folder, config | Ordered slice list | `slides` |
| `preprocess` | Create the low-res registration image and the **tissue mask** per slice; the mask defines what counts as tissue for every later stage (debris, labels and bubbles removed) | `SlideReader` | `reg/{id}.tif`, `reg/{id}.json`, `mask/{id}.png` | `slides`, `store` |
| `registration` | Per consecutive pair: boundary-based initial alignment from the masks, keypoint detection inside the masks, feature matching and robust (RANSAC) transform fit, boundary fallback; chain to the reference slice. Drift correction post-MVP | Reg images, masks | `transforms/pair_{id}.json`, `transforms/chained_{id}.json` (transform + match quality) | OpenCV, `store` |
| `qc` | Quality metrics inside the tissue only (feature inliers and residual, masked correlation, mask overlap), overlays with tissue outlines, flagging. Manual overrides post-MVP | Reg images, masks, transforms | `qc/report.json`, overlay images | `registration` |
| `volume` | Resample slices through transforms tile by tile into the pyramid, **tissue only**: masks applied, background removed, canvas limited to the tissue | Slides, transforms, masks, voxel size | `volume.zarr` | `slides`, `registration`, zarr |
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
    PRE -->|reg images + tissue masks| REG[feature-based registration<br/>inside tissue masks]
    REG -->|transforms + match quality| QC[qc]
    QC -.->|flagged pairs, post-MVP| OVR[manual overrides]
    OVR -.-> REG
    REG -->|transforms| VOL[volume builder<br/>tissue only]
    PRE -->|tissue masks| VOL
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
class SlideReader:                     # implemented (M2)
    @classmethod open(path: Path) -> SlideReader
    levels: list[LevelInfo]            # index, downsample, width, height
    mpp: tuple[float, float]           # microns per pixel at level 0
    level_for_downsample(downsample: float) -> int   # level counts differ per file: never index by position
    read_region(level: int, x: int, y: int, w: int, h: int) -> ndarray  # RGB uint8, (x, y) in level pixels; transparent -> white
    close() -> None

# catalog
class Catalog:                         # implemented (M2)
    @classmethod scan(data_dir: Path, id_range: tuple[int, int] | None = None) -> Catalog
    slices: list[SliceRef]             # id (int N), path, in order; raises on gaps or duplicates
class ProjectConfig:                   # data_dir, work_dir, z_spacing_um, base_downsample, n_levels
    @classmethod load(path: Path) -> ProjectConfig
# Voxel size is not on Catalog (as first drafted): x/y come from the slides' mpp, z from ProjectConfig.

# --- M3/M4 entries below are proposed; exact contracts are fixed in task M3-00 ---

# preprocess                         # M3
make_reg_image(slice: SliceRef, work: WorkDir, reader_factory=SlideReader.open, downsample=32) -> Path
make_mask(reg_image: Path, work: WorkDir) -> Path   # mandatory (F12): uint8 {0,255}, same shape; defines "tissue"

# registration                       # M3, feature-based (F13), tissue only (F12)
RegImage                             # registration image + mask + metadata (downsample, mpp), loaded from the work dir
initial_from_masks(fixed: RegImage, moving: RegImage) -> Rigid2D     # boundary-based: centroid + principal axes
detect_features(img: RegImage) -> Features                           # SIFT keypoints + descriptors inside the mask only
match_and_fit(fixed: Features, moving: Features, init: Rigid2D) -> PairResult   # ratio test + RANSAC
register_pair(fixed: RegImage, moving: RegImage) -> PairResult
    # PairResult: transform (moving -> fixed, µm), method ("features" | "boundary"), n_matches, n_inliers, rms_um
chain(pairs: dict[int, Rigid2D]) -> dict[int, Rigid2D]   # slice -> reference (first slice of the range)
Rigid2D: matrix(), inverse(), compose(), apply(points), to_pixels(downsample, mpp), save(), load()
correct_drift(...)                                       # post-MVP

# qc                                 # M4, inside the tissue only
score_pair(fixed: RegImage, moving: RegImage, result: PairResult) -> PairScore   # inliers, rms_um, masked NCC, mask Dice
render_overlay(fixed: RegImage, moving: RegImage, t: Rigid2D, out: Path) -> Path # green/magenta + tissue outlines
apply_override(...)                                      # post-MVP

# store                              # implemented (M2)
WorkDir(root).manifest, .volume        # the only place that knows file names
Manifest(path).get(key), .set(key, value)   # JSON, atomic write

# volume                             # implemented (M2), without transforms
VolumeSpec(z_spacing_um, base_downsample=4, n_levels=5, chunk_xy=512, shard_xy=4096)
build_volume(catalog, out: Path, spec: VolumeSpec, manifest: Manifest | None = None,
             reader_factory=SlideReader.open, overwrite: bool = False) -> Path
    # M3 adds (proposed, fixed in M3-00):
    #   transforms: dict[int, Rigid2D] | None = None   (by slice id; None = slices centred, as in M2)
    #   masks: dict[int, Path] | None = None           (tissue only, F12: voxels outside the warped mask
    #                                                    + margin = fill value 255; canvas = bounding box of
    #                                                    the transformed tissue, not of the scanned area;
    #                                                    tissue voxels never pure white: 255 is reserved for background)
    # Raises VolumeExistsError if `out` exists with other settings and overwrite is False.

# serve                              # implemented (M2)
create_app(root: Path) -> FastAPI      # static files of the work dir, CORS + range requests
neuroglancer_state(volume_url, ome) -> dict   # RGB layer; OME axis c mapped to channel dim "c^"
                                              # M3-12: shader shows pure white (background) as transparent
launch_local_viewer(state, port=0) -> neuroglancer.Viewer   # served from 127.0.0.1; caller keeps it alive (weak refs)
GET /volume.zarr/{path}                # Zarr metadata and shard files
GET /tile/{z}/{level}/{x}/{y}          # post-MVP, native-resolution tile

# pipeline (CLI)
brainrec --config C volume --slices A-B [--overwrite]   # interim (M2)
brainrec --config C serve [--port 8000]                # interim (M2); viewer on port+1
brainrec run [--stage S] [--slices A-B]                # M5
brainrec status                                        # M5
brainrec qc                                            # M4
```

## 6. Technology decisions

| Concern | Choice | Rationale | Trade-off |
|---|---|---|---|
| Language | Python 3.12+ | Best ecosystem for whole-slide images, registration and Zarr | Slower than C++; heavy loops run in native libraries |
| NDPI reading | OpenSlide (`openslide-python` + `openslide-bin`), tifffile as fallback | Lazy tile access to NDPI; `openslide-bin` bundles the native library in the venv | |
| Storage | OME-Zarr 0.5 on Zarr v3, sharded (512 px chunks in 4096 px shards), zstd lossless | Chunked, multi-resolution, resumable; shards keep the file count low on the exFAT drive (512 KB blocks, a `._` file per file) | Lossless only: Neuroglancer's Zarr reader has no JPEG codec (see docs/decisions/0001) |
| Image ops | numpy, scikit-image, OpenCV | Standard and fast | |
| Registration | OpenCV: SIFT features inside the tissue masks, ratio-test matching, RANSAC fit (`estimateAffinePartial2D` or a rigid fit on the inliers); tissue-boundary initialisation and fallback from the masks. SimpleITK only for an optional intensity refinement | Owner requirement (F13); feature matching is rotation-invariant (no angle sweep) and robust to outliers; OpenCV is already a dependency | Consecutive sections do not share the same cells, so matches must come from larger structures at about 7 µm/px; feature-poor pairs fall back to boundary alignment |
| Parallelism | `concurrent.futures`; Dask only if needed | Simplicity | |
| Server | FastAPI + uvicorn | Lightweight, testable | |
| Viewer | Neuroglancer (MVP), served locally by the `neuroglancer` Python package | Solves 3D streaming and orthogonal views, saving weeks; local serving avoids a public page reading from localhost | Less control over UX; replaceable, isolated to `serve/` |
| Config | YAML + pydantic | Validated and versionable | |
| Packaging | `pyproject.toml` (uv or pip) | Reproducible | |
| Testing | pytest, synthetic slice generator | Independent tests without the real data | |

## 7. Project structure

```
Brain 3D Reconstruction/
├── pyproject.toml
├── README.md, CLAUDE.md
├── config/project.example.yaml   (copy to config/project.yaml, git-ignored)
├── requirements-lock.txt         (exact versions, from pip freeze)
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

- **Storage:** about 423 GB free on My Passport (402 GB of it already holds the sources). Raw RGB is about 43 GB per slice at native resolution, about 6 TB for 140, so native resolution cannot be precomputed and must be served on demand (post-MVP). Measured in M2 (lossless zstd): the 4x base level is about 1.45 GB of data per slice, about 2 GB with the pyramid, so **about 280 GB for all 140 slices**. That fits, with limited margin. The 2x floor (about 4x larger) would not fit losslessly.
- **exFAT drive:** 512 KB allocation blocks, and macOS writes a `._` file per written file (for the `com.apple.provenance` attribute). Many small files waste space (measured 3x) and make directory listings take minutes. Mitigated by sharding; any future output format must also keep the file count low.
- **Drive disconnects:** the drive dropped out once during an M2 build. Builds resume per slice; a disconnect costs at most one slice.
- **Tissue mask errors (F12):** the mask now decides what is kept. Too tight cuts tissue away; too loose keeps glass, debris, labels or bubbles. Mitigated by a safety margin (about 50 µm, proposed), dropping small components, and mask outlines on the contact sheet and QC overlays for owner review.
- **Coarse tissue edge:** the mask comes from the 32x image (about 7 µm/px), so at 0.88 µm/px the tissue edge follows the mask's steps, softened by the margin. A finer edge is a later improvement.
- **Feature matching on histology (F13):** consecutive sections do not contain the same cells; matches must come from larger structures (vessels, layers, the tissue edge). Repetitive texture can produce false matches; RANSAC and the inlier count guard against them, with boundary alignment as fallback. Measured in the real-data spike (risk R10).
- **Drift:** errors accumulate across 140 chained pairs; mitigated by drift correction and QC; unsolved in general.
- **Non-rigid distortion:** can deform real anatomy; kept behind a flag.
- **Damaged sections** (tears, folds) will need manual overrides.
- **Anisotropy:** Z spacing is much larger than in-plane pixel size, so side views look like stacked slices.
- **Viewer:** Neuroglancer needs a compatible Zarr layout and CORS; its UX may not suit.
- **I/O:** the external drive is the speed limit; limit passes over each source slice.
- **Environment:** OpenSlide needs a native install; nothing is installed until approved.
