# MVP definition

Status: **approved** (2026-10-19). Detailed tasks: [tasks/BACKLOG.md](../tasks/BACKLOG.md).

## 1. Goal

Prove the whole chain on real data at reduced resolution: **10 consecutive NDPI slices in, a registered 3D volume out, explored in the browser.** Full scale (140 slices) is a matter of run time once the chain works; the chain itself is the risk.

## 2. In scope

| Component | MVP scope |
|---|---|
| `slides` | Read NDPI metadata and pyramid levels; read regions at a level chosen by downsample factor (OpenSlide). |
| `catalog` | Scan the data folder, order by `N_H`, check gaps and duplicates, select a slice range; YAML project config. |
| `store` | Work-directory layout and a JSON manifest for resuming. |
| `preprocess` | A **tissue mask** per slice (from colour saturation; debris removed, detached fragments kept) that defines what counts as tissue for all later stages, and a grayscale registration image (about 7 µm/px, 32x). |
| `registration` | **Feature-based, tissue only (F12, F13):** boundary-based initial alignment from the masks; ORB features inside the tissue, matched between neighbours with guided two-pass matching, coarse-to-fine (64x, then 32x); robust (RANSAC) fit of a **similarity** transform (rotation, uniform scale, translation); boundary alignment as fallback; chained to slice 1. Transforms and match quality saved as small JSON files. |
| `qc` | Per pair, inside the tissue only: feature inliers and residual, masked correlation, mask overlap; an overlay with tissue outlines; flagging below a threshold. No manual override. |
| `volume` | Resample slices through their transforms into an OME-Zarr 0.5 pyramid (base 4x = 0.88 µm/px, 5 levels, sharded, lossless), **tissue only**: background outside the masks removed, volume limited to the tissue extent. |
| `serve` | Static, CORS-enabled hosting of the work directory; local Neuroglancer viewer link. |
| `viewer` | Neuroglancer: orthogonal views, 3D view, zoom; the background outside the tissue is shown transparent. |
| `pipeline` | `brainrec run`, `brainrec status`, `brainrec qc`, with per-slice resume. |
| tests | Synthetic slices with known placement and known rigid transforms; unit tests per component; one end-to-end test. |

## 3. Out of scope (postponed)

All 140 slices; non-rigid registration; global drift correction; manual overrides; custom viewer; on-demand native-resolution (220 nm) tiles and the `/tile` endpoint; multi-process parallelism and Dask; stain normalisation; automatic handling of tears and folds; packaging, installers, authentication; lossy storage formats.

## 4. End-to-end workflow

```
brainrec --config config/project.yaml run --slices 1-10
  catalog    -> ordered list of 10 slices
  preprocess -> reg image + tissue mask per slice (32x)
  register   -> per consecutive pair: boundary alignment, ORB features inside the tissue,
                guided matching (64x, then 32x) + RANSAC similarity fit; chained to slice 1
  qc         -> score (inside tissue) + overlay with tissue outlines per pair, flags
  volume     -> volume.zarr (OME-Zarr 0.5, 4x base, 5 levels), tissue only
brainrec --config config/project.yaml serve   -> local Neuroglancer link, open in Chrome
```

## 5. Risks to test early

| # | Risk | Tested in | Status |
|---|---|---|---|
| R1 | NDPI read speed from My Passport | M1 | **Resolved:** coarse levels in under 1.5 s |
| R2 | Which pyramid levels exist | M1 | **Resolved:** 1, 2, 4, 8 ... always present; level count varies (9 to 11) |
| R3 | OpenSlide reads these NDPI files | M1 | **Resolved:** yes, all 140 |
| R4 | Volume size and storage | M1, M2 | **Resolved:** about 2 GB/slice lossless, about 280 GB for 140 slices; exFAT needs sharding |
| R5 | Rigid registration aligns real adjacent slices | M3 | **Resolved for slices 1 to 10 (M3-11, owner review 2026-11-03):** rigid is not enough (spike: residual up to 242 µm); the similarity fit (with uniform scale) aligns all 9 pairs (residual 16 to 19 µm) and tissue stays in place through z. Damaged margins need non-rigid (post-MVP) |
| R6 | Neuroglancer opens and streams the OME-Zarr | M2 | **Resolved:** works in Chrome with hardware acceleration on |
| R7 | Slice orientation (flips) | M3 | **Resolved for slices 1 to 10 (spike, confirmed in M3-11):** no mirrored slices |
| R8 | Z spacing unknown | n/a | Open: placeholder 5 µm in config |
| R9 | Tissue masks decide what is kept: no tissue cut off, no glass or debris kept (F12) | M3 | **Resolved for slices 1 to 10 (M3-04, M3-11):** the registered volume shows only tissue, detached fragments included, no glass, labels or debris and nothing cut off (owner review 2026-11-03) |
| R10 | Real neighbouring sections yield enough reliable feature matches (F13) | M3 | **Resolved for slices 1 to 10 with guided matching (M3-11):** all 9 pairs `features`, 53 to 925 inliers; global matching alone is not enough (spike) |

## 6. Acceptance criteria

The MVP is accepted when all hold, each checked by a command or test that was actually executed, with the result reported:

| ID | Criterion | How verified |
|---|---|---|
| AC1 | `brainrec run --slices 1-10` completes on the real data from a clean work directory; a second run skips finished work. | Run log; second run reads no slice pixels |
| AC2 | Source NDPI files are unchanged. | Fingerprint (size, mtime, SHA-256) before and after |
| AC3 | All outputs are inside `/Volumes/My Passport/3D Brain Reconstruction/`; total size reported. | `du`, listing |
| AC4 | On synthetic data with known shifts, rotations (including about 45° and 170°) and uniform scale changes (up to ±10%), the feature-based registration recovers them within 1 px (at registration resolution), 0.5° and 0.3% scale, using only features inside the tissue: synthetic debris on the background does not change the result. | pytest |
| AC5 | On real slices 1 to 10, each consecutive overlay shows matching tissue outlines and internal structures (judged by the project owner); every pair has a score computed inside the tissue, and pairs below the threshold are flagged. | QC report + review |
| AC6 | The volume opens in Neuroglancer in Chrome, with orthogonal and 3D views; zooming loads finer levels without errors. | Project owner, in the browser |
| AC7 | At the base level (0.88 µm/px), tissue structure is visible down to cell scale; the achievable resolution is stated plainly. | Project owner, in the browser |
| AC8 | Documentation matches the implementation; measured numbers replace estimates. | Review |
| AC9 | **Tissue only:** outside the tissue masks every voxel is background (and not stored); the volume's extent is the tissue's, not the scanned glass; no tissue is cut off at the edges (a background rim of up to about 50 µm is accepted); in the viewer the background is transparent. | pytest (synthetic masks) + owner review on slices 1 to 10 |

## 7. Milestones

| # | Milestone | Gate | Status |
|---|---|---|---|
| M0 | Environment and project setup | `pytest`, `ruff`, `mypy` run clean | Done (CI still to do) |
| M1 | Data characterisation | Owner chose the base level (4x) | Done |
| M2 | Unregistered vertical slice (10 slices, no registration) | Volume opens in Neuroglancer (R6) | Done |
| M3 | Registration (feature-based, tissue only) | Synthetic tests pass (AC4, AC9 part); real result reviewed (R5, R7, R9, R10) | Done (2026-11-03) |
| M4 | Quality control | Owner can see which pairs are doubtful (AC5) | To do |
| M5 | Integration: `brainrec run` end to end | AC1, AC2, AC3, AC6 | To do |
| M6 | MVP review | All AC met; owner sign-off | To do |

```mermaid
flowchart LR
    M0 --> M1 --> M2 --> M3 --> M5 --> M6
    M3 --> M4 --> M5
```

M4 can start as soon as the transform model of M3 exists (task M3-05); it does not need real registration results until its integration task.
