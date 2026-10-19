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

- Source: 140 NDPI files, about 3.78 GB each (about 530 GB total), in `/Volumes/My Passport/3D FCD Alignment/`, read in place (never copied).
- Sample slice: `Data Sample/250_23_II16_1_H - 2024-11-11 15.08.23.ndpi`.
- The external drive is likely to be the I/O bottleneck.
- Free space: 467 GB on My Passport, about 26 GB on the internal disk.
- NDPI is a proprietary TIFF variant. First slice (reported by the project owner): 139776 x 102144 px, 220 nm/pixel (115454 DPI), about 30.75 x 22.47 mm, about 14.3 Gpx (about 43 GB raw RGB). Other slices are expected to be similar. Pyramid levels not yet inspected.
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
