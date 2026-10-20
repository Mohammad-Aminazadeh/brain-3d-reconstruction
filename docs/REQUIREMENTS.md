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
| F10 | Tissue/background mask per slice (required by F12) | Confirmed |
| F11 | Drift correction over the chain of 140 slices | Proposed |
| F12 | **Tissue only.** The empty glass around the tissue is ignored: alignment uses only tissue, and the final volume contains only tissue (background removed, volume limited to the tissue extent) | Confirmed (owner, 2026-10-19) |
| F13 | **Feature-based alignment.** Alignment detects edges and features (tissue boundaries and internal structures) in each image, finds the corresponding features in consecutive images and aligns the images by them | Confirmed (owner, 2026-10-19) |

## 2. Non-functional requirements

| Area | Requirement | Status |
|---|---|---|
| Memory | Never load a full slice at full resolution (24 GB RAM machine); work on pyramid levels or tiles | Forced by data |
| Storage | Outputs must fit the available space (about 423 GB free on the external drive; 26 GB free internally) | Forced by data |
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
- Free space: about 423 GB on My Passport (measured 2026-10-19), about 26 GB on the internal disk.
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
7. Tissue only (F12): the background is excluded from registration (features and metrics inside the tissue mask only) and from the volume (voxels outside the mask are background; the volume is limited to the tissue extent). See `docs/decisions/0003-tissue-only-feature-based-registration.md`.
8. Registration is feature-based (F13): tissue-boundary alignment for the initial estimate, then keypoint features matched between neighbouring slices with a robust (RANSAC) fit; boundary alignment as fallback. Intensity-based refinement only if the results show it is needed.
9. Tissue edge in the MVP: a background rim of up to about 50 µm along the tissue edge is acceptable, so that tissue is never cut off (owner, 2026-10-19).
10. The background outside the tissue is shown **transparent** in the viewer, so empty space contrasts with the tissue (owner, 2026-10-19).
11. **Similarity transform** (rotation, uniform scale, translation) instead of rigid only (owner, 2026-10-20). Spike: rigid residual up to 242 µm against 16 to 19 µm with similarity.
12. **Guided two-pass ORB matching** is the main registration method: boundary-based estimate, then a 2.5 mm search window, then a 0.3 mm window. No mirror handling, since no mirrored slices were found (owner, 2026-10-20).
13. **Coarse-to-fine registration:** 64x for the first pass and 32x for the second, both from the same 32x read (owner, 2026-10-20).
14. **Detached tissue fragments count as tissue** and are kept (owner, 2026-10-20). Masks keep every component above a small absolute size (debris specks are below 0.2 mm²) and include pale tissue connected to clearly stained tissue.
15. **Damaged margins** (folds, tears, compression) may stay hundreds of µm off in the MVP; non-rigid correction comes after the MVP (owner, 2026-10-20).
16. **Overlapping ripped tissue** (folds and flaps where layers lie on top of each other, recognisable by their darker colour) may be ignored: it need not be included in the mask (owner, 2026-10-20). Not removed explicitly in the MVP.

## 5. Proposed assumptions awaiting approval

1. Registration uses a similarity transform (rotation, uniform scale, translation) in the MVP; affine and non-rigid later. (Approved 2026-10-20, from the spike results; replaces "rigid first")
2. Precomputed volume resolution floor of about 1 µm/pixel (0.88 or 0.44 µm/px, see open questions); native resolution served on demand, after the MVP. (Approved)
3. Neuroglancer as the MVP viewer. (Approved)
4. MVP proves the pipeline on about 10 consecutive slices before processing all 140. (Approved)
5. Voxel size (including Z spacing) is configuration metadata. (Approved)
6. Generated data lives in `/Volumes/My Passport/3D Brain Reconstruction/`, outside the repository. (Approved)
7. Outside-tissue voxels are stored as pure white (255, 255, 255; the volume's fill value), so all-background chunks are not stored at all. Tissue voxels that are exactly pure white are stored as 254, so pure white means background unambiguously and the viewer can show it transparent (decision 10). (Approved)
8. The volume uses the registration mask (32x, about 7 µm/px) warped with the slice's transform and enlarged by a safety margin of about 50 µm, so tissue is never cut off; a thin rim of glass may remain along the edge. A finer tissue edge is a later improvement. (Approved, see decision 9)
9. Feature detector: SIFT from OpenCV (rotation- and scale-invariant; already a dependency). ORB is compared in the spike; AKAZE is not available in the installed OpenCV 5.0 build. (Proposed)

## 6. Open questions

1. Exact section thickness and spacing (µm), to be obtained from the lab.
2. NDPI pyramid levels (dimensions and pixel size are known for slice 1; to be verified for the rest).
3. Resolution floor: 4x (0.88 µm/px) accepted for now. 2x does not fit losslessly; it would need a lossy viewer format.
4. Can the native-resolution on-demand path read the external drive fast enough? To be measured.
5. Which viewer to use if Neuroglancer's UX proves unsuitable?

## 7. Measured facts (M1, `scripts/characterize.py`, read-only)

Measured on 2026-10-19 on the real files in `/Volumes/My Passport/3D FCD Alignment/`.

- **All 140 files present**, numbered 1 to 140 with no gaps; total 402 GB (not 530 GB).
- **Same scanner settings everywhere:** 220.5 nm/px at level 0, 40x lens, Hamamatsu vendor.
- **Slice size varies a lot** (tissue shrinks towards the end of the block): width 71680 to 157696 px (median 132608), height 48384 to 107520 px (median 91392). Examples: slice 1 is 139776 x 102144, slice 20 is 146944 x 107520, slice 70 is 129024 x 96768, slice 140 is 89600 x 48384. The "all slices similar" assumption is **false**: the volume canvas must be the bounding box over all slices, with per-slice offsets.
- **Pyramid level count varies** (9, 10 or 11 levels). Levels 0 to 3 always have downsample 1, 2, 4, 8. **Code must select levels by downsample factor, never by index.**
- **Read speed** (2048 x 2048 crop at the tissue centre): about 0.07 s from the internal disk, about 0.5 to 1.5 s from My Passport at levels 0 to 2 on a cold read; whole levels of up to 14 Mpx read in under 0.1 s, and a 56 Mpx level (16x) in about 0.4 to 1.3 s. Coarse levels are not a bottleneck.
- **Compressibility** (M1 estimate, JPEG q90): 0.3 to 0.4 bytes/px. Not usable as such: Neuroglancer cannot read JPEG-compressed Zarr (see `docs/decisions/0001-volume-storage-format.md`).
- **Measured volume size (M2, lossless zstd, sharded):** slices 1 to 10 take 19.5 GB for all 5 levels (about 1.95 GB per slice; level 0 alone about 1.45 GB), in 960 files. Slices 1 to 10 are among the largest, so 140 slices should need at most about 275 GB.
- **Measured build speed (M2):** about 1.1 min per slice from My Passport (11.5 min for 10 slices); 140 slices would take about 2.5 hours.
- **Work drive:** My Passport is exFAT with 512 KB allocation blocks; macOS writes a `._` file per written file. Free space measured at 423 to 436 GB (it varies with other use of the drive). The drive disconnected once during a build.
- **Orientation** of slices (flips) not yet examined.
- **Tissue boundaries** are clearly visible in all images (owner, 2026-10-19); this is the basis for F12 and the boundary-based initial alignment. Mask quality on real slices (debris, labels, bubbles) not yet measured.
- **OpenCV build** (installed 5.0.0): SIFT, ORB and RANSAC fitting (`estimateAffinePartial2D`) available; AKAZE not available.

### Spike results (M3-06, 2026-10-19, `scripts/spike_registration.py`, slices 1 to 10, all 9 neighbouring pairs)

Outputs (moved on 2026-10-20 for presentation) in `/Volumes/My Passport/3D Brain Reconstruction/results/M3-06_registration_spike/` (29 files, 84 MB); compressed copies in the repository under `results/` (see `results/README.md`): `contact_sheet.png`, `overlay_<k>.png` and `matches_<k>.png` (global matching), `overlay_guided_<k>.png` (guided matching), `results.json`. Sources were only read (32x level; 16x and 64x for one comparison).

- **Orientation (R7):** no mirrored slices among 1 to 10. Visually consistent orientation; strong pairs give 42 to 186 inliers normally against 3 to 10 when mirrored. Rotations between neighbours are small (0.7° to 5°).
- **Tissue masks (R9):** Otsu on colour saturation plus cleanup gives one tissue region per slice, 370 to 427 mm². Scan-edge streaks and most debris are excluded; folds are included. Pale detached fragments at the left of slices 6 to 10 were excluded; owner decision: keep them (decision 14). Cause: fragments of 0.6 to 2.8 mm² pass the threshold but were removed by the 1%-of-tissue rule (about 4.2 mm²), and their pale parts lie at the Otsu threshold (saturation about 18; glass 9 to 10, tissue 27 to 30).
- **Tissue size differs between sections:** slices 1 to 5 have about 390 mm² of tissue, slices 6 to 10 about 425 mm² (slice 5: 370 mm²). Slice 5 is compressed, folded and torn.
- **Feature matching (R10):**
  - *Global matching* (ratio test over the whole image) is weak: SIFT keeps 4 to 58 inliers per pair, ORB 11 to 186. The uniform tissue texture makes most correct matches ambiguous, and fits with few inliers are unreliable.
  - *Guided matching* works:
    - **Pass 1** searches a 2.5 mm window around the boundary-based estimate.
    - **Pass 2** searches a 0.3 mm window around the pass-1 fit.
    - **Inliers:** ORB keeps 62 to 882 per pair, SIFT 24 to 291. ORB beats SIFT on every pair.
    - **Speed:** about 5 s per pair at 32x.
- **Boundary-based initial alignment:**
  - **Shape overlap (IoU):** 0.83 to 0.98.
  - **Agreement with the feature result:** within 0.2° to 6.4° in angle and 3 to 900 µm at the centroid.
  - **Use:** good enough to seed guided matching, not as a final result.
- **Rigid vs similarity (R5, C3):** on the same inliers, the rigid residual is 18 to 242 µm and the similarity residual (with uniform scale) 16 to 19 µm.
  - **Scale factors:** 0.997 to 1.013 for most pairs. Pair 5→6 is 0.91, because slice 5 is compressed.
  - **Conclusion:** rigid is clearly insufficient for 7 of 9 pairs; similarity is needed.
- **Resolution:**
  - **64x:** most guided ORB inliers (544 vs 299 at 32x for pair 1→2; 108 vs 62 for pair 5→6), and fastest.
  - **16x:** worst.
  - **Interpretation:** the correspondence between sections lies in larger structures, not in fine detail.
  - **Scale depends on resolution for the damaged pair 5→6** (0.87 at 16x, 0.91 at 32x, 0.96 at 64x). That is a sign of uneven deformation.
- **Accuracy to expect:**
  - **Undamaged areas:** the inlier residual is about 17 µm, roughly 2.5 registration pixels or 20 base pixels at 0.88 µm/px. That is structure-level alignment: vessels and layers line up; individual cells do not correspond between sections anyway.
  - **Damaged areas:** at folds, tears and compressed margins (slices 4 and 5), edges stay off by hundreds of µm after any global transform. The best mask overlap after guided matching is 0.84 for pairs 4→5 and 5→6, against 0.93 to 0.98 for undamaged pairs. Fixing this needs non-rigid registration (post-MVP).
