# Requirements

Status labels: **Confirmed** (stated or approved by the project owner), **Proposed** (assumption awaiting confirmation).

## 1. Functional requirements

| ID | Requirement | Status |
|---|---|---|
| F1 | Read NDPI files from the external drive, ordered by the `N_H` number in the filename | Confirmed |
| F2 | Align the 140 slices so that corresponding tissue lines up between consecutive slices | Confirmed |
| F3 | Build a 3D volume from the aligned slices | Confirmed |
| F4 | Present the volume in a UI | Confirmed |
| F5 | Zoom in and out down to the level where individual cells are visible | Confirmed |
| F6 | Resumable, per-slice batch processing | Proposed |
| F7 | Registration QC: metrics, overlays, flagging of bad pairs, manual overrides | Proposed |
| F8 | Orthogonal slice views (XY/XZ/YZ), slice slider and a 3D view | Proposed |
| F9 | Intermediate results stored on disk so stages need not be recomputed | Proposed |
| F10 | Tissue/background mask per slice | Proposed |
| F11 | Drift correction over the chain of 140 slices | Proposed |

## 2. Non-functional requirements

| Area | Requirement | Status |
|---|---|---|
| Memory | Never load a full slice at full resolution (24 GB RAM machine); work on pyramid levels or tiles | Forced by data |
| Storage | Outputs must fit the available space (467 GB free on the external drive; 26 GB free internally) | Forced by data |
| Quality | Preserve cellular detail at the highest resolution made available | Confirmed |
| Interactivity | Smooth pan and zoom with progressive loading | Proposed |
| Reproducibility | Same input and parameters give the same output; parameters stored with results | Proposed |
| Non-destructive | Source NDPI files are never modified | Proposed |
| Modularity / testability | Independent components with defined interfaces, testable on small synthetic crops | Confirmed |
| Simplicity | Prefer simple solutions, no microservices | Confirmed |
| Platform | macOS, Apple Silicon (M4 Pro, 24 GB RAM) | Inferred |

## 3. Technical constraints

- Source: 140 NDPI files, 0.97 to 3.8 GB each (402 GB total, measured), in `/Volumes/My Passport/3D FCD Alignment/`, read in place (never copied).
- Sample slice: `Data Sample/250_23_II16_1_H - 2024-11-11 15.08.23.ndpi`.
- The external drive is likely to be the I/O bottleneck.
- Free space: 467 GB on My Passport, about 26 GB on the internal disk.
- NDPI is a proprietary TIFF variant. First slice (reported by the project owner): 139776 x 102144 px, 220 nm/pixel (115454 DPI), about 30.75 x 22.47 mm, about 14.3 Gpx (about 43 GB raw RGB). Other slices differ: see section 7 (measured in M1).
- In-plane voxel size is therefore 0.22 um; Z spacing is unknown (micrometre order).
- Section thickness and spacing are on the order of micrometres; the exact value is unknown.
- One section per slide, the same stain on all slides, no reference data (no blockface photos, no MRI).

## 4. Confirmed decisions

1. Quality goal: low-resolution whole-brain view, with full-resolution tiles streamed on demand.
2. Viewer: browser-based.
3. Pipeline language: Python.
4. Architecture: staged batch pipeline plus a thin tile server, a single Python package, no microservices.
5. Registration works on low-resolution images; transforms are stored separately from pixels.
6. Volume is stored as a chunked multi-resolution OME-Zarr store.

## 5. Proposed assumptions awaiting approval

1. Rigid (plus affine) registration first; non-rigid behind a flag later.
2. Precomputed volume resolution floor of about 1 µm/pixel (0.88 or 0.44 µm/px, see open questions); native resolution served on demand, after the MVP. (Approved)
3. Neuroglancer as the MVP viewer. (Approved)
4. MVP proves the pipeline on about 10 consecutive slices before processing all 140. (Approved)
5. Voxel size (including Z spacing) is configuration metadata. (Approved)
6. Generated data lives in `/Volumes/My Passport/3D Brain Reconstruction/`, outside the repository. (Approved)

## 6. Open questions

1. Exact section thickness and spacing (µm), to be obtained from the lab.
2. NDPI pyramid levels (dimensions and pixel size are known for slice 1; to be verified for the rest).
3. Exact precomputed resolution floor (about 0.88 µm/px at 4x down, or about 0.44 µm/px at 2x), decided after measuring real compression.
4. Can the native-resolution on-demand path read the external drive fast enough? To be measured.
5. Which viewer to use if Neuroglancer's UX proves unsuitable?

## 7. Measured facts (M1, `scripts/characterize.py`, read-only)

Measured on 2026-10-19 on the real files in `/Volumes/My Passport/3D FCD Alignment/`.

- **All 140 files present**, numbered 1 to 140 with no gaps; total 402 GB (not 530 GB).
- **Same scanner settings everywhere:** 220.5 nm/px at level 0, 40x lens, Hamamatsu vendor.
- **Slice size varies a lot** (tissue shrinks towards the end of the block): width 71680 to 157696 px (median 132608), height 48384 to 107520 px (median 91392). Examples: slice 1 is 139776 x 102144, slice 20 is 146944 x 107520, slice 70 is 129024 x 96768, slice 140 is 89600 x 48384. The "all slices similar" assumption is **false**: the volume canvas must be the bounding box over all slices, with per-slice offsets.
- **Pyramid level count varies** (9, 10 or 11 levels). Levels 0 to 3 always have downsample 1, 2, 4, 8. **Code must select levels by downsample factor, never by index.**
- **Read speed** (2048 x 2048 crop at the tissue centre): about 0.07 s from the internal disk, about 0.5 to 1.5 s from My Passport at levels 0 to 2 on a cold read; whole levels of up to 14 Mpx read in under 0.1 s, and a 56 Mpx level (16x) in about 0.4 to 1.3 s. Coarse levels are not a bottleneck.
- **Compressibility** (JPEG q90 of a tissue-centre crop, so an upper bound per pixel): about 0.3 to 0.4 bytes/px. Estimated upper bound per slice for 140 slices: native about 4.5 GB, 2x about 1.3 GB, 4x about 0.35 GB. For the whole volume (140 slices, plus about 33% for the pyramid): **4x about 65 GB, 2x about 240 GB**, if stored with a JPEG-class codec. Zarr with a lossless codec would be several times larger; the codec choice is open.
- **Orientation** of slices (flips) not yet examined.
