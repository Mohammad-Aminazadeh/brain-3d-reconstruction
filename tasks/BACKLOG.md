# MVP backlog (Work Breakdown Structure)

Scope and acceptance criteria (AC1 to AC8): [docs/MVP.md](../docs/MVP.md). Architecture and interfaces: [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

Hierarchy: **Milestone → Component → Task → Subtask.** One task = one focused coding session with a verifiable result. Tasks are done one at a time (CLAUDE.md); "independent" below means the order among them is free.

**Status:** Done · In progress · To do · Blocked. **Complexity:** Low · Medium · High (High tasks are split into subtasks, each about Low/Medium).

Conventions that apply to every task (not repeated below):
- Tests run in the session and reported with their real output; `ruff check` and `mypy` clean.
- Only the files listed under "Files" change, plus docs when an interface or behaviour changes.
- Any interface change is explained (consequences) before it is made.
- Tests never read the 140 real files; the only real-data test is the optional sample-slide test.

---

## Summary

| ID | Task | Component | Cx | Status | Depends on |
|---|---|---|---|---|---|
| M0-01 | Repository and GitHub remote | project | Low | Done | none |
| M0-02 | Python environment and OpenSlide | project | Low | Done | none |
| M0-03 | Package skeleton and `pyproject.toml` | project | Low | Done | M0-02 |
| M0-04 | Lint, type-check and lock file | project | Low | Done | M0-03 |
| M0-05 | Fake slide test helper | tests | Low | Done | M0-03 |
| M0-06 | Continuous integration (GitHub Actions) | project | Low | To do | M0-04 |
| M1-01 | Characterisation script | scripts | Low | Done | M0-02 |
| M1-02 | Header survey script (all slides) | scripts | Low | To do | M0-02 |
| M1-03 | Record measured facts | docs | Low | Done | M1-01 |
| M2-01 | `SlideReader` | slides | Low | Done | M0-03 |
| M2-02 | `Catalog.scan` | catalog | Low | Done | M0-03 |
| M2-03 | `ProjectConfig` | catalog | Low | Done | M0-03 |
| M2-04 | `WorkDir` and `Manifest` | store | Low | Done | M0-03 |
| M2-05 | Unregistered volume builder | volume | High | Done | M2-01, M2-02, M2-04, M0-05 |
| M2-06 | Static data server | serve | Low | Done | M0-03 |
| M2-07 | Neuroglancer state and local viewer | serve | Medium | Done (unverified in browser) | M2-06 |
| M2-08 | Interim CLI (`volume`, `serve`) | pipeline | Low | Done | M2-03, M2-05, M2-07 |
| M2-09 | Real build, slices 1 to 10 | integration | Low | Done | M2-08 |
| M2-10 | Visual check in Neuroglancer | integration | Low | Done | M2-09 |
| M2-11 | Close M2 test gaps (config, CLI) | tests | Low | To do | M2-08 |
| M3-01 | Work-dir layout for reg, mask, transforms, qc | store | Low | To do | M2-04 |
| M3-02 | Synthetic tissue and rigid-transform generator | tests | Medium | To do | M0-05 |
| M3-03 | Registration image | preprocess | Medium | To do | M3-01 |
| M3-04 | Tissue mask | preprocess | Low | To do | M3-03 |
| M3-05 | `Rigid2D` transform model | registration | Medium | To do | M0-03 |
| M3-06 | Orientation spike (flips) | integration | Low | To do | M3-03 |
| M3-07 | Rigid pairwise registration | registration | High | To do | M3-02, M3-04, M3-05 |
| M3-08 | Chain and register a slice range | registration | Low | To do | M3-07, M3-01 |
| M3-09 | Volume builder with transforms | volume | High | To do | M3-05, M2-05 |
| M3-10 | Interim CLI: `preprocess`, `register` | pipeline | Low | To do | M3-08, M3-09 |
| M3-11 | Real registration, slices 1 to 10 | integration | Medium | To do | M3-10, M2-10, M3-06 |
| M4-01 | Pair score | qc | Medium | To do | M3-05, M3-02 |
| M4-02 | Overlay image | qc | Low | To do | M3-05 |
| M4-03 | QC report and flags | qc | Low | To do | M4-01, M4-02, M3-01 |
| M4-04 | `brainrec qc` command | pipeline | Low | To do | M4-03 |
| M4-05 | Real QC review, slices 1 to 10 | integration | Medium | To do | M4-04, M3-11 |
| M5-01 | Stage runner with resume | pipeline | Medium | To do | M3-10, M4-04 |
| M5-02 | `brainrec run` and `brainrec status` | pipeline | Medium | To do | M5-01 |
| M5-03 | Disk-space preflight | pipeline | Low | To do | M2-04 |
| M5-04 | Source fingerprint (before/after) | scripts | Low | To do | M2-02 |
| M5-05 | End-to-end synthetic integration test | tests | Medium | To do | M5-02 |
| M5-06 | End-to-end real run and resume check | integration | Medium | To do | M5-02, M5-03, M5-04 |
| M6-01 | Documentation consistency pass | docs | Low | To do | M5-06 |
| M6-02 | Acceptance report | docs | Low | To do | M5-06 |
| M6-03 | Scale-up plan (planning only) | docs | Low | To do | M5-06 |
| M6-04 | Owner sign-off | review | Low | To do | M6-01, M6-02, M6-03 |

---

## Independent and sequential work

**Independent (order is free; each can be picked up whenever its listed dependency is done):**
- Any time: M0-06 (CI), M1-02 (header survey), M2-11 (test gaps), M5-03 (disk preflight), M5-04 (source fingerprint).
- After M3-01: M3-02 (synthetic generator), M3-03 (registration image) and M3-05 (transform model) do not depend on each other.
- After M3-05: M3-09 (volume with transforms), M4-01 and M4-02 (QC score and overlay) can be built and tested on synthetic data while M3-07 is in progress.
- M3-06 (orientation spike) needs only M3-03.

**Sequential (critical path):**
M3-05 → M3-07 → M3-08 → M3-10 → M3-11 → M4-05 → M5-01 → M5-02 → M5-06 → M6-01/02/03 → M6-04.
M2-10 (owner's visual check) must pass before M3-11, because M3-11 relies on the viewer to judge registration.

```mermaid
flowchart TD
    M2-05 --> M3-09
    M2-04 --> M3-01
    M3-01 --> M3-03 --> M3-04
    M3-03 --> M3-06
    M0-05 --> M3-02
    M3-02 --> M3-07
    M3-04 --> M3-07
    M3-05 --> M3-07
    M3-05 --> M3-09
    M3-05 --> M4-01
    M3-05 --> M4-02
    M3-07 --> M3-08 --> M3-10
    M3-09 --> M3-10
    M2-10 --> M3-11
    M3-06 --> M3-11
    M3-10 --> M3-11
    M4-01 --> M4-03
    M4-02 --> M4-03 --> M4-04
    M3-11 --> M4-05
    M4-04 --> M4-05
    M4-04 --> M5-01
    M3-10 --> M5-01
    M5-01 --> M5-02
    M5-02 --> M5-05
    M5-02 --> M5-06
    M5-03 --> M5-06
    M5-04 --> M5-06
    M4-05 --> M5-06
    M5-06 --> M6-01 & M6-02 & M6-03 --> M6-04
```

---

## M0 Environment and project setup

### Component: project

#### M0-01 Repository and GitHub remote · Low · Done
- **Objective:** Version control with a private GitHub remote.
- **Inputs:** project folder. **Outputs:** git repo on `main`, remote `Mohammad-Aminazadeh/brain-3d-reconstruction` (private), `.gitignore` excluding data, venv, local config.
- **Depends on:** none. **Files:** `.gitignore`.
- **Acceptance:** `git remote -v` shows the remote; no `.ndpi`, `.zarr` or `.venv` tracked.
- **Tests:** `git ls-files` contains no data files.

#### M0-02 Python environment and OpenSlide · Low · Done
- **Objective:** Reproducible environment on macOS (Apple Silicon).
- **Inputs:** Python 3.13. **Outputs:** `.venv` with dependencies; OpenSlide via `openslide-bin`.
- **Depends on:** none. **Files:** none in the repo.
- **Acceptance:** `import openslide` works without environment variables; the sample slide opens (139776 x 102144, 9 levels).
- **Tests:** `tests/integration/test_reader_sample.py` (added in M2-01).

#### M0-03 Package skeleton and `pyproject.toml` · Low · Done
- **Objective:** Installable `brainrec` package with one sub-package per component.
- **Outputs:** `src/brainrec/{slides,catalog,preprocess,registration,qc,volume,serve,store,pipeline}/`, `tests/`, `config/`, `scripts/`, `docs/decisions/`; `brainrec` console script.
- **Depends on:** M0-02. **Files:** `pyproject.toml`, package `__init__.py` files.
- **Acceptance:** `pip install -e ".[dev]"` succeeds; `pytest` runs.
- **Tests:** `tests/unit/test_import.py`.

#### M0-04 Lint, type-check and lock file · Low · Done
- **Objective:** Enforce conventions and pin exact versions.
- **Outputs:** ruff and mypy configuration in `pyproject.toml`; `requirements-lock.txt`.
- **Depends on:** M0-03. **Files:** `pyproject.toml`, `requirements-lock.txt`.
- **Acceptance:** `ruff check src tests scripts` and `mypy` report no issues.
- **Tests:** the two commands themselves.

#### M0-06 Continuous integration (GitHub Actions) · Low · To do
- **Objective:** Run ruff, mypy and pytest on every push, so "tests pass" is checkable by anyone.
- **Inputs:** `requirements-lock.txt`. **Outputs:** `.github/workflows/ci.yml` (Ubuntu, Python 3.13).
- **Depends on:** M0-04. **Files:** `.github/workflows/ci.yml`, README badge line.
- **Acceptance:** the workflow runs green on GitHub; the sample-slide test is skipped there (no data in CI).
- **Tests:** the workflow run.

### Component: tests

#### M0-05 Fake slide test helper · Low · Done
- **Objective:** In-memory stand-in for `SlideReader` so components are testable without NDPI files.
- **Outputs:** `tests/synthetic/fake_slide.py` (`FakeSlide`: gray rectangle, black marker, levels 1/2/4/8; `FakeFactory`: counts reads).
- **Depends on:** M0-03. **Files:** `tests/synthetic/fake_slide.py`.
- **Acceptance:** used by the volume tests; satisfies the `SliceSource` protocol (mypy).
- **Tests:** exercised by `tests/unit/test_volume.py`.

---

## M1 Data characterisation

### Component: scripts

#### M1-01 Characterisation script · Low · Done
- **Objective:** Measure levels, pixel size, read speed and compressibility of real slides (read-only).
- **Inputs:** NDPI paths. **Outputs:** `scripts/characterize.py`; numbers in `docs/REQUIREMENTS.md` section 7.
- **Depends on:** M0-02. **Files:** `scripts/characterize.py`.
- **Acceptance:** runs on the sample and on slides from My Passport without writing anything.
- **Tests:** manual run (script, not library code).

#### M1-02 Header survey script (all slides) · Low · To do
- **Objective:** Make the all-140 header survey (run ad hoc in M1) reproducible.
- **Inputs:** data folder. **Outputs:** `scripts/survey_headers.py` printing count, gaps, size range, pixel sizes, level counts and downsample patterns.
- **Depends on:** M0-02. **Files:** `scripts/survey_headers.py`.
- **Acceptance:** reproduces the M1 numbers (140 files, width 71680 to 157696, 9/10/11 levels, 0.2205 µm/px).
- **Tests:** manual run against My Passport; output pasted into the task report.

### Component: docs

#### M1-03 Record measured facts · Low · Done
- **Objective:** Replace estimates with measurements.
- **Outputs:** `docs/REQUIREMENTS.md` section 7. **Depends on:** M1-01.
- **Acceptance:** owner chose the 4x base level from these numbers.
- **Tests:** none (documentation).

---

## M2 Unregistered vertical slice

Goal: data path and viewer proven with the least code: 10 slices stacked as they are.

### Component: slides

#### M2-01 `SlideReader` · Low · Done
- **Objective:** Read-only NDPI access in level-local coordinates.
- **Inputs:** NDPI path. **Outputs:** `levels`, `mpp`, `level_for_downsample()`, `read_region()` (RGB uint8, transparent to white), `close()`.
- **Depends on:** M0-03. **Files:** `src/brainrec/slides/reader.py`.
- **Acceptance:** levels selected by downsample factor, never by index.
- **Tests:** `tests/integration/test_reader_sample.py` (skipped when the sample is absent).

### Component: catalog

#### M2-02 `Catalog.scan` · Low · Done
- **Objective:** Discover and order slices by `N_H`; select a range.
- **Outputs:** `Catalog`, `SliceRef`. **Depends on:** M0-03. **Files:** `src/brainrec/catalog/scan.py`.
- **Acceptance:** numeric order; errors on gaps in the range, duplicates, empty folder.
- **Tests:** `tests/unit/test_catalog.py` (5 tests).

#### M2-03 `ProjectConfig` · Low · Done
- **Objective:** Validated YAML configuration.
- **Outputs:** `ProjectConfig.load()`; `config/project.example.yaml`. **Depends on:** M0-03.
- **Files:** `src/brainrec/catalog/config.py`, `config/project.example.yaml`.
- **Acceptance:** loads the example; `z_spacing_um` marked as placeholder.
- **Tests:** missing, see M2-11.

### Component: store

#### M2-04 `WorkDir` and `Manifest` · Low · Done
- **Objective:** One place for work-dir file names; JSON progress record with atomic writes.
- **Depends on:** M0-03. **Files:** `src/brainrec/store/{layout,manifest}.py`.
- **Acceptance:** manifest survives reopen; parent folders created.
- **Tests:** `tests/unit/test_manifest.py`.

### Component: volume

#### M2-05 Unregistered volume builder · High · Done
- **Objective:** Stack slices, centred, into a sharded OME-Zarr 0.5 pyramid, resumable per slice.
- **Inputs:** `Catalog`, `VolumeSpec`, `Manifest`, reader factory. **Outputs:** `volume.zarr` (axes c, z, y, x; uint8; 5 levels from the slides' own pyramid).
- **Depends on:** M2-01, M2-02, M2-04, M0-05. **Files:** `src/brainrec/volume/builder.py`.
- **Subtasks:**
  - M2-05a Header pass: dimensions per level, single pixel size enforced, canvas, aligned offsets.
  - M2-05b Array creation: Zarr v3, 512 chunks in 4096 shards, zstd, white fill; OME 0.5 metadata.
  - M2-05c Band writing aligned to the shard grid (each shard written once).
  - M2-05d Resume via manifest signature; refuse to overwrite a different volume unless `overwrite=True`.
- **Acceptance:** pixels placed exactly; all levels aligned; resume reads nothing on a finished build; existing volume protected.
- **Tests:** `tests/unit/test_volume.py` (12 tests: bands, placement, full content, level alignment, metadata, sharding, resume, interruption, overwrite guard, pixel-size check).

### Component: serve

#### M2-06 Static data server · Low · Done
- **Objective:** Serve the work directory with CORS and range requests.
- **Depends on:** M0-03. **Files:** `src/brainrec/serve/app.py`.
- **Acceptance:** cross-origin GET and range GET work.
- **Tests:** `tests/unit/test_serve.py` (CORS, range).

#### M2-07 Neuroglancer state and local viewer · Medium · Done (unverified in browser)
- **Objective:** Show the RGB volume in a locally served Neuroglancer.
- **Inputs:** volume URL, OME metadata. **Outputs:** viewer state (channel axis `c` mapped to `c^`, scales in metres); local viewer URL; public fallback link.
- **Depends on:** M2-06. **Files:** `src/brainrec/serve/app.py`; `docs/decisions/0002-local-viewer.md`.
- **Acceptance:** state has the expected source, dimensions and shader; rendering confirmed in M2-10.
- **Tests:** `tests/unit/test_serve.py` (state mapping, URL round trip, local viewer page answers 200).

### Component: pipeline

#### M2-08 Interim CLI (`volume`, `serve`) · Low · Done
- **Objective:** Run M2 from the command line until `brainrec run` exists (M5).
- **Depends on:** M2-03, M2-05, M2-07. **Files:** `src/brainrec/pipeline/cli.py`.
- **Acceptance:** `brainrec --config C volume --slices 1-10 [--overwrite]`; `brainrec --config C serve`.
- **Tests:** missing, see M2-11.

### Component: integration

#### M2-09 Real build, slices 1 to 10 · Low · Done
- **Objective:** Build the unregistered volume from My Passport.
- **Outputs:** `/Volumes/My Passport/3D Brain Reconstruction/volume.zarr`; build time and size recorded in docs.
- **Depends on:** M2-08.
- **Acceptance:** exit code 0; manifest lists 10 done slices; size measured.
- **Tests:** run log.
- **Result (2026-10-19):** exit 0, 10 slices in 11.5 min (about 1.1 min/slice), 960 files, 19.5 GB on disk (no block waste). Server smoke test: data and viewer pages answer 200; shard range reads answer 206.

#### M2-10 Visual check in Neuroglancer · Low · Done
- **Objective:** Confirm R6: the volume opens and streams in the browser (MVP gate for M2).
- **Inputs:** `brainrec serve` link. **Outputs:** owner's confirmation, or a bug report with a screenshot.
- **Depends on:** M2-09.
- **Acceptance:** RGB colours correct; 4-panel view; zoom loads finer levels; no errors in the Neuroglancer status bar.
- **Tests:** manual (owner, Chrome).
- **First attempt (2026-10-19):** colours correct, 4-panel view shown, no status-bar errors. But loading was slow, not every area sharpened, then all panels went black and Chrome reported "WebGL not supported".
  - Evidence: three Chrome crash dumps of the **GPU process** (Chrome 154, Apple GPU, ANGLE Metal backend) at 20:29, 20:30 and 20:31; Chrome disables WebGL after repeated GPU-process crashes until it is restarted. No server errors (8,422 requests, 7,847 at full resolution). System memory normal.
  - My Passport serves about 27 random 430 KB chunks/s (median 0.5 s at 16 in parallel), so full-resolution areas fill in slowly.
  - Change: viewer now opens in a single `xy` panel with `gpuMemoryLimit` 512 MB, `systemMemoryLimit` 1 GB and 8 concurrent downloads (`brainrec serve --layout 4panel --gpu-memory-mb N` to override).
- **Second attempt (2026-10-19):** after a full Chrome restart, with hardware acceleration switched on, everything worked in the `xy` layout. The owner reports that Chrome's hardware acceleration had been **off** during the first attempt, so WebGL was probably running in software; that is the most likely cause of the slowness and the GPU-process crashes. 4-panel retest (same 512 MB limit): works. Owner noted that 3D zoom centres on the current position and only the area shown by the 2D panels becomes sharp in 3D; both are Neuroglancer behaviour (documented under README, Viewer controls).

### Component: tests

#### M2-11 Close M2 test gaps (config, CLI) · Low · To do
- **Objective:** Cover the two untested pieces of M2.
- **Depends on:** M2-08. **Files:** `tests/unit/test_config.py`, `tests/unit/test_cli.py`.
- **Acceptance:** config: loads valid YAML, rejects a missing field; CLI: `--slices 3-7` parses to (3, 7), `--slices 5` to (5, 5), and `volume` on an existing different volume exits with an error message instead of a traceback.
- **Tests:** the two new files.

---

## M3 Registration

Goal: rigid slice-to-slice alignment, proven on synthetic data (AC4), then applied to slices 1 to 10. Transforms are expressed in **micrometres in each slice's level-0 frame**, so they apply unchanged at every pyramid level.

### Component: store

#### M3-01 Work-dir layout for reg, mask, transforms, qc · Low · To do
- **Objective:** Define every MVP output path in one place before the stages that write them.
- **Outputs:** `WorkDir.reg_image(id)`, `.reg_meta(id)`, `.mask(id)`, `.pair_transform(id)`, `.chained_transform(id)`, `.qc_report`, `.qc_overlay(id)`.
- **Depends on:** M2-04. **Files:** `src/brainrec/store/layout.py`, `docs/ARCHITECTURE.md` (layout section).
- **Acceptance:** all paths under `root`; names stable and documented.
- **Tests:** `tests/unit/test_layout.py` (paths, no collisions between ids).

### Component: tests

#### M3-02 Synthetic tissue and rigid-transform generator · Medium · To do
- **Objective:** Ground truth for registration and QC tests.
- **Outputs:** `tests/synthetic/tissue.py`: `make_tissue(shape, seed)` (irregular blob with texture and dark "cells"), `apply_rigid(img, angle_deg, tx, ty)` returning the moved image and true parameters; a `FakeSlide` variant built from a given image.
- **Depends on:** M0-05. **Files:** `tests/synthetic/tissue.py`, `tests/synthetic/fake_slide.py` (additive).
- **Acceptance:** deterministic for a seed; applying a transform and its inverse returns the original within interpolation error.
- **Tests:** `tests/unit/test_synthetic.py`.

### Component: preprocess

#### M3-03 Registration image · Medium · To do
- **Objective:** One small grayscale image per slice for registration.
- **Inputs:** `SliceRef`, reader factory, downsample (32, about 7 µm/px). **Outputs:** `reg/{id}.tif` (uint8, tissue bright on dark background: 255 minus luminance) and `reg/{id}.json` (downsample, µm/px, size).
- **Depends on:** M3-01. **Files:** `src/brainrec/preprocess/regimage.py`; `src/brainrec/slides/` (receives the `SliceSource` protocol, moved from `volume`; `volume` re-exports it).
- **Subtasks:** M3-03a move `SliceSource` to `slides` (interface location change, no behaviour change); M3-03b `make_reg_image()`.
- **Acceptance:** reads one whole coarse level (no level-0 access); output size equals the level size; metadata correct.
- **Tests:** `tests/unit/test_preprocess.py` with `FakeSlide` (size, inversion, metadata, reads only the requested level).

#### M3-04 Tissue mask · Low · To do
- **Objective:** Restrict registration and QC to tissue.
- **Inputs:** registration image. **Outputs:** `mask/{id}.png` (0/255).
- **Method:** Otsu threshold, morphological close and open, fill holes, drop components below 1% of tissue area.
- **Depends on:** M3-03. **Files:** `src/brainrec/preprocess/mask.py`.
- **Acceptance:** Dice at least 0.95 against the synthetic ground-truth blob.
- **Tests:** `tests/unit/test_mask.py`.

### Component: registration

#### M3-05 `Rigid2D` transform model · Medium · To do
- **Objective:** The transform type every later stage uses.
- **Outputs:** `Rigid2D(angle_rad, tx_um, ty_um, cx_um, cy_um)` mapping slice coordinates to reference coordinates; `matrix()`, `inverse()`, `compose()`, `apply(points)`, `to_pixels(downsample, mpp)` (3x3 matrix at a pyramid level), `save()`/`load()` (JSON with schema version).
- **Depends on:** M0-03. **Files:** `src/brainrec/registration/transform.py`, `docs/ARCHITECTURE.md` (interface).
- **Acceptance:** level-independent: the same transform gives consistent pixel matrices at 4x and 32x.
- **Tests:** `tests/unit/test_transform.py` (identity, inverse round trip, composition order, JSON round trip, pixel conversion at two levels).

#### M3-07 Rigid pairwise registration · High · To do
- **Objective:** Estimate the rigid transform aligning slice i+1 to slice i.
- **Inputs:** two registration images and masks with metadata. **Outputs:** `Rigid2D` plus final metric value.
- **Depends on:** M3-02, M3-04, M3-05. **Files:** `src/brainrec/registration/pairwise.py`.
- **Subtasks:**
  - M3-07a Initialisation: align mask centroids (SimpleITK moments initialiser). Test: pure translation recovered within 2 px.
  - M3-07b Coarse rotation search: score candidate angles every 10 degrees over 360 at reduced size; keep the best. Test: rotations of 45 and 170 degrees land within 10 degrees.
  - M3-07c Refinement: SimpleITK `Euler2DTransform`, Mattes mutual information, masks, 3-level pyramid. Test: AC4 (1 px, 0.5 degree) on 5 seeded synthetic pairs.
  - M3-07d Result conversion: pixels to micrometres in `Rigid2D`; return metric. Test: conversion matches M3-05 conventions.
  - M3-07e Mirrored candidate, **only if M3-06 finds flipped slices**. Test: a mirrored synthetic pair is recovered.
- **Acceptance:** AC4 passes; an unrelated image pair yields a clearly worse metric than a true pair.
- **Tests:** `tests/unit/test_pairwise.py`.

#### M3-08 Chain and register a slice range · Low · To do
- **Objective:** Pairwise transforms for consecutive slices, composed to slice 1's frame.
- **Inputs:** catalog range, work dir. **Outputs:** `transforms/pair_{id}.json`, `transforms/chained_{id}.json` (slice 1 = identity).
- **Depends on:** M3-07, M3-01. **Files:** `src/brainrec/registration/chain.py`.
- **Acceptance:** chained = composition of pairs in the documented order; per-pair resume via manifest.
- **Tests:** `tests/unit/test_chain.py` (known synthetic chain of 4 transforms; resume skips done pairs).

#### M3-06 Orientation spike (flips) · Low · To do
- **Objective:** Resolve risk R7 before real registration: are some slices mirrored?
- **Inputs:** registration images of slices 1 to 10. **Outputs:** a contact sheet (`qc/contact_sheet.png`) and a short finding in `docs/REQUIREMENTS.md` section 7.
- **Depends on:** M3-03. **Files:** `scripts/contact_sheet.py`, `docs/REQUIREMENTS.md`.
- **Acceptance:** owner and assistant agree whether M3-07e is needed.
- **Tests:** manual review.

### Component: volume

#### M3-09 Volume builder with transforms · High · To do
- **Objective:** Resample slices through their chained transforms into the pyramid.
- **Inputs:** `Catalog`, chained transforms, `VolumeSpec`. **Outputs:** `volume.zarr` as in M2, registered.
- **Interface change:** `build_volume(..., transforms: dict[int, Rigid2D] | None = None)`; `None` keeps M2 behaviour (centred). Consequence: the manifest signature includes a hash of the transforms, so changed transforms trigger the overwrite guard.
- **Depends on:** M3-05, M2-05. **Files:** `src/brainrec/volume/builder.py`, `src/brainrec/volume/warp.py` (new), `docs/ARCHITECTURE.md`.
- **Subtasks:**
  - M3-09a Canvas from the bounding box of all transformed slice corners (µm), aligned offsets. Test: known transforms give the expected canvas.
  - M3-09b Output tiles equal to shards (4096 x 4096): for each tile, the inverse-mapped source bounding box, clipped to the slice. Test: rotated tile maps to the expected source box; memory bounded (box at most about 5800 px square).
  - M3-09c Warp one tile: read the source box, `cv2.warpAffine` (bilinear, white border), write the tile. Test: integer translation reproduces pixels exactly; 90 degree rotation moves the marker to the expected place.
  - M3-09d Apply per level using the same `Rigid2D`. Test: the marker is at the same physical position at all 5 levels.
  - M3-09e Signature and resume with transforms. Test: unchanged transforms resume; changed ones are refused without overwrite.
- **Acceptance:** with synthetic slices moved by known transforms, the inverse transforms put all markers on one (x, y) across z within 1 px at the base level.
- **Tests:** `tests/unit/test_volume_warp.py`; existing `tests/unit/test_volume.py` still passes unchanged.

### Component: pipeline

#### M3-10 Interim CLI: `preprocess`, `register` · Low · To do
- **Objective:** Run M3 stages on real data before the M5 runner exists.
- **Outputs:** `brainrec preprocess --slices A-B`, `brainrec register --slices A-B`; `brainrec volume` uses chained transforms when they exist (`--unregistered` to force M2 mode).
- **Depends on:** M3-08, M3-09. **Files:** `src/brainrec/pipeline/cli.py`, README usage.
- **Acceptance:** each command runs on a synthetic temp work dir.
- **Tests:** `tests/unit/test_cli.py` (extended).

### Component: integration

#### M3-11 Real registration, slices 1 to 10 · Medium · To do
- **Objective:** Resolve risk R5 on real data.
- **Inputs:** My Passport slices 1 to 10. **Outputs:** transforms, registered volume, metric per pair, run times.
- **Depends on:** M3-10, M2-10, M3-06.
- **Acceptance:** in Neuroglancer, scrolling through z shows tissue staying in place (owner's judgement); any pair that fails is listed with its metric.
- **Tests:** run log; owner review.

---

## M4 Quality control

### Component: qc

#### M4-01 Pair score · Medium · To do
- **Objective:** A number per pair that separates good from bad alignment.
- **Inputs:** two registration images, masks, `Rigid2D`. **Outputs:** `PairScore(ncc, dice)`: normalised cross-correlation inside the mask overlap, and Dice of the warped masks.
- **Depends on:** M3-05, M3-02. **Files:** `src/brainrec/qc/metrics.py`.
- **Acceptance:** identical images score 1.0; a 10-pixel misalignment scores clearly lower; unrelated images score lowest.
- **Tests:** `tests/unit/test_qc_metrics.py`.

#### M4-02 Overlay image · Low · To do
- **Objective:** Visual check per pair.
- **Outputs:** `qc/overlay_{id}.png`: fixed slice in green, warped moving slice in magenta (aligned tissue looks gray/white).
- **Depends on:** M3-05. **Files:** `src/brainrec/qc/overlay.py`.
- **Acceptance:** correct size; channels assigned as documented.
- **Tests:** `tests/unit/test_qc_overlay.py`.

#### M4-03 QC report and flags · Low · To do
- **Objective:** One report listing every pair, its scores and a flag.
- **Inputs:** scores; thresholds `qc_min_ncc`, `qc_min_dice` in `ProjectConfig`. **Outputs:** `qc/report.json`.
- **Depends on:** M4-01, M4-02, M3-01. **Files:** `src/brainrec/qc/report.py`, `src/brainrec/catalog/config.py` (two fields).
- **Acceptance:** every consecutive pair present; flag set exactly when a score is below its threshold.
- **Tests:** `tests/unit/test_qc_report.py`.

### Component: pipeline

#### M4-04 `brainrec qc` command · Low · To do
- **Objective:** Produce scores, overlays and report for a slice range; print a summary table.
- **Depends on:** M4-03. **Files:** `src/brainrec/pipeline/cli.py`.
- **Acceptance:** runs on a synthetic temp work dir; exit code 0; summary lists flagged pairs.
- **Tests:** `tests/unit/test_cli.py` (extended).

### Component: integration

#### M4-05 Real QC review, slices 1 to 10 · Medium · To do
- **Objective:** AC5 on real data; calibrate thresholds.
- **Outputs:** QC report for 9 pairs; chosen thresholds in the example config; owner's verdict per flagged pair.
- **Depends on:** M4-04, M3-11.
- **Acceptance:** owner confirms the overlays of unflagged pairs look aligned.
- **Tests:** owner review.

---

## M5 Integration

### Component: pipeline

#### M5-01 Stage runner with resume · Medium · To do
- **Objective:** Run stages in order (preprocess → register → qc → volume), each resumable, from one call.
- **Outputs:** `pipeline/runner.py`: `Stage` protocol (`name`, `run(range, ctx)`, `status(range, ctx)`); `run_pipeline(range, stages, ctx)`; injected reader factory for tests.
- **Depends on:** M3-10, M4-04. **Files:** `src/brainrec/pipeline/runner.py`, `src/brainrec/pipeline/stages.py`.
- **Subtasks:** M5-01a stage protocol and context; M5-01b wrap the four stages; M5-01c runner with stop-on-error and status.
- **Acceptance:** a failure in one stage stops later stages and leaves earlier results valid.
- **Tests:** `tests/unit/test_runner.py` (order, skip done, stop on error, using fakes).

#### M5-02 `brainrec run` and `brainrec status` · Medium · To do
- **Objective:** The MVP command line.
- **Outputs:** `run --slices A-B [--stage S] [--overwrite]`, `status`, `qc`, `serve`. The interim `preprocess`, `register` and `volume` commands are removed (documented in README and ARCHITECTURE).
- **Depends on:** M5-01. **Files:** `src/brainrec/pipeline/cli.py`, README, `docs/ARCHITECTURE.md`.
- **Acceptance:** `status` shows per-stage progress per slice.
- **Tests:** `tests/unit/test_cli.py` (rewritten for the final commands).

#### M5-03 Disk-space preflight · Low · To do
- **Objective:** Refuse to start a volume build that cannot fit (CLAUDE.md data-safety rule).
- **Outputs:** `estimate_volume_bytes(spec, dims)` (about 2 GB per slice at 4x, from M2 measurements) and a check against free space with a 10% margin.
- **Depends on:** M2-04. **Files:** `src/brainrec/store/space.py`.
- **Acceptance:** clear error message with needed and free space.
- **Tests:** `tests/unit/test_space.py` (free space mocked).

### Component: scripts

#### M5-04 Source fingerprint (before/after) · Low · To do
- **Objective:** Evidence for AC2 (sources unchanged).
- **Outputs:** `scripts/fingerprint.py`: size and mtime for all slides, SHA-256 for a given range; `compare` mode.
- **Depends on:** M2-02. **Files:** `scripts/fingerprint.py`.
- **Acceptance:** `compare` reports zero differences after a run.
- **Tests:** `tests/unit/test_fingerprint.py` (temp files; detects a changed byte).

### Component: tests

#### M5-05 End-to-end synthetic integration test · Medium · To do
- **Objective:** The whole pipeline in one automated test.
- **Inputs:** 4 synthetic slides moved by known rigid transforms. **Outputs:** `tests/integration/test_end_to_end.py`.
- **Depends on:** M5-02.
- **Acceptance:** after `run`, markers align across z within 1 px at the base level; QC flags none; second run reads no pixels.
- **Tests:** the test itself.

### Component: integration

#### M5-06 End-to-end real run and resume check · Medium · To do
- **Objective:** AC1, AC2, AC3, AC6 on real data.
- **Steps:** fingerprint; clean work dir; `run --slices 1-10`; re-run (must skip); fingerprint compare; `du` of outputs; `serve` and owner check.
- **Depends on:** M5-02, M5-03, M5-04, M4-05.
- **Acceptance:** all four criteria met, evidence recorded in M6-02.
- **Tests:** logs and owner review.

---

## M6 MVP review

### Component: docs

#### M6-01 Documentation consistency pass · Low · To do
- **Objective:** AC8: docs match code; measured numbers replace estimates.
- **Files:** README, `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/MVP.md`, this backlog.
- **Acceptance:** every interface in ARCHITECTURE exists with that signature; statuses current.
- **Tests:** review; `pytest`, `ruff`, `mypy` clean.

#### M6-02 Acceptance report · Low · To do
- **Objective:** One page of evidence per acceptance criterion.
- **Outputs:** `docs/MVP_REPORT.md` (AC1 to AC8: command, result, date, open issues).
- **Acceptance:** owner can verify each criterion from the report.

#### M6-03 Scale-up plan (planning only) · Low · To do
- **Objective:** Plan the step from the MVP to 140 slices; no implementation.
- **Outputs:** `docs/SCALE_UP.md`: run time and storage for 140 slices (from measured numbers), and the order of the postponed features with their risks.
- **Acceptance:** owner can choose the next milestone from it.

### Component: review

#### M6-04 Owner sign-off · Low · To do
- **Objective:** Formal MVP acceptance.
- **Depends on:** M6-01, M6-02, M6-03.
- **Acceptance:** owner approves or lists required changes.
