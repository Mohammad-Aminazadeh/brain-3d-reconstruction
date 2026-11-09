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
| F14 | **Centre first.** The tissue centre must align as precisely as possible; errors may grow towards the boundary, where they matter less, but are minimised everywhere (see `docs/decisions/0004-centre-first-best-effort-registration.md`) | Confirmed (owner, 2026-11-04) |
| F15 | **No stop on a bad pair.** A pair that cannot be aligned reliably does not stop the run: it gets the best possible alignment and is flagged for review | Confirmed (owner, 2026-11-04) |
| F16 | **Cell detection.** Every nucleus in the tissue is detected in its section, measured (centre, radius, outline, shape, staining, nucleolus visible or not) and classified by its appearance into configurable classes. **Every cell gets a class** (its most likely one) **and a confidence**; there is no "unclassified" (owner, 2026-11-08). Classes are proposed by automatic grouping of the cells' measured looks and confirmed by experts. Accuracy is measured against labelled cells and must be good enough to find suspicious cells later | Confirmed (owner, 2026-11-05 and 2026-11-08); class list and thresholds Proposed |
| F17 | **Cells in 3D.** Each cell is shown as a 3D blob centred on its nucleus at its registered position, sized from its measured radius, with colour and shape per type. Types can be shown and hidden; the tissue layer can be faded down to fully transparent, so that only the structures remain visible | Confirmed (owner, 2026-11-05) |
| F18 | **Traceable cells.** Each cell keeps its measurements, its outline and its position in the source slide, so it can be opened at full resolution (0.22 µm/px) without detecting again | Confirmed (owner, 2026-11-06) |
| F19 | **One cell, one blob.** A cell cut by several consecutive sections is linked across them into one 3D cell; its size comes from its largest profile | Confirmed (owner, 2026-11-06: all sections scanned, none left out; to be confirmed by the lab) |
| F20 | **Cell table.** All cells with 3D position, type, size and measurements are exported as a table (CSV) and per slide for QuPath (GeoJSON), for review by a pathologist | Confirmed (owner, 2026-11-06) |
| F21 | **Density maps.** A 3D density map per cell type (coarse grid, about 50 µm) shows cell-rich and cell-poor regions, layers and the lesion; densities are corrected for section thickness once it is known | Confirmed (owner, 2026-11-06) |
| F22 | **Vessels and other structures in 3D** (milestone M8). Vessels are also used as landmarks for a local, non-rigid refinement of the alignment | Confirmed (owner, 2026-11-05 and 2026-11-06); which structures: open |
| F23 | **Suspicious cells** (milestone M9): cells that meet criteria given by the supervisor are flagged and listed for review | Confirmed (owner, 2026-11-05); criteria open |

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
- Section thickness and spacing are on the order of micrometres; the exact value is unknown. The owner will supply the thickness later (2026-11-06); until then it stays an open configuration value, and results that depend on it (z proportions, 3D cell size, densities per mm³) are marked as provisional.
- **All sections were scanned, none left out** (owner, 2026-11-06, as far as known; to be confirmed by the lab, question Q2). The scanned sections are therefore consecutive, and the spacing equals the thickness.
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
17. **Cells as 3D blobs** (owner, 2026-11-05): a detected nucleus becomes a blob with the same centre and a radius from its 2D size; types differ in colour and shape. Cells are separate viewer layers; the image volume is unchanged. See `docs/decisions/0005-cells-in-3d.md`.
18. **Cells first, then vessels and other structures, then suspicious cells** (owner, 2026-11-05): milestones M7, M8, M9.
19. **Ideas accepted** (owner, 2026-11-06):
    - linking a cell's profiles across consecutive sections;
    - a nucleolus flag per cell, so that size can be judged on profiles cut close to the middle;
    - the cell table with CSV and QuPath export;
    - 3D density maps per type;
    - vessels in 3D and as landmarks for a local, non-rigid refinement;
    - the density correction for section thickness (Abercrombie);
    - **not done:** invented images or cells between sections; a separate mesh per cell.
20. **Section thickness stays open** (owner, 2026-11-06). Implementation continues without it; it is added to the configuration when the lab supplies it.
21. **Cell classes by appearance** (owner, 2026-11-08, decision 0006):
    - every cell is classified, with a confidence; nothing is "unclassified";
    - the class list comes from both an automatic grouping of the measured looks (M7-18) and expert confirmation;
    - the first labelling round uses the fine list (assumption 12).
22. **Terms.**
    - *Background* means only the glass outside the tissue (shown transparent, C11).
    - The image volume is the *tissue layer*. Its opacity can be lowered to zero, which makes "the rest of the tissue" transparent. No separate name is needed.
    - *Structures* are detected objects: cells now, vessels and others in M8.
    - A *profile* is the cut surface of one cell in one section; a *cell* is one or more linked profiles.
23. **Long runs ask before each next slide** (owner, 2026-11-08): detection of all 140 slices takes about 39 h. `brainrec detect` therefore asks after each slide whether to start the next one; a later run continues with the slides not yet done. `--no-ask` runs without asking.
24. **The interim classifier's reference lives in the code** (owner, 2026-11-08): `src/brainrec/detect/rules_v1.json` (2 KB), versioned with the code like a calibrated constant.
25. **Detection QC** (owner, 2026-11-09, M7-16): a separate command `brainrec qc-cells`, report `qc/cells_report.json` and one review sheet per slice. Slices are compared with up to two neighbours on each side. Folds are dark regions of at least 2× the median tissue density on the 32x level, over at least 0.05 mm². The thresholds are in `docs/ARCHITECTURE.md` (C9, C10b).
26. **Cell-scale alignment by votes of nucleus offsets** (owner, 2026-11-09, M7-12): method and the settings `align_window_um` 150, `align_search_um` 100 and `align_min_peak_ratio` 2.0, see decision 0007.
27. **All sections are consecutive, with no gaps** (owner, 2026-11-09, interim until the lab answers Q28): the volume keeps a uniform z spacing. Pairs without recurring nuclei get no links.

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
10. **Cell detection resolution:** cells are detected at the 2x level (0.44 µm/px); the spike (M7-01) checks 1x and 4x. (Proposed)
11. **What a cell is in this stain:** the nucleus, which is the clearly visible circle; neuron bodies are faint. (Proposed, question Q10)
12. **Starting appearance classes** (owner, 2026-11-08, for the first labelling round until Q9 is answered; may change after M7-18):
    - neuron, astrocyte-like, oligodendrocyte-like, microglia-like, vessel-wall cell, other;
    - suspicious classes later (M9): dysmorphic neuron, balloon cell;
    - all in a configurable type table.
13. **Blob shape:** a sphere with the cell's radius. Types differ by colour and optionally by a fixed proportion, as axis-aligned ellipsoids. Shapes turned like the 2D outline would need meshes and are not planned. (Proposed, question Q26)
14. **Accuracy gates** on held-out labelled crops: detection F1 ≥ 0.90 and median radius error ≤ 15%; targets per type are set after the spike. (Proposed, question Q18)
15. **Detector choice (decided after M7-01, owner 2026-11-07):** InstanSeg at 2x is the working detector, StarDist the second candidate; the final choice, fine-tuning and possibly a combination follow from the labelled test crops (M7-10); Cellpose 4 is dropped for speed; nucleolus and exact size from 1x patches around larger nuclei; a sharpness score per tile marks uncertain cells. *Original proposal:* a baseline without new dependencies (hematoxylin colour separation, threshold, split of touching nuclei) is compared with pretrained deep-learning detectors (Cellpose, StarDist or InstanSeg). Installing them needs permission. (Proposed, question Q19)
16. **Local alignment for linking** is computed from the detected nuclei (feature-based, on top of the registration), on a grid of windows; target accuracy 1 to 2 µm. Intensity correlation may only initialise or refine it (F13). (Approved 2026-11-09 as votes of nucleus offsets, decision 0007)

## 6. Open questions

### 6.1 Technical

1. Exact section thickness and spacing (µm), to be obtained from the lab (questions Q1 and Q2 below).
2. NDPI pyramid levels (dimensions and pixel size are known for slice 1; to be verified for the rest).
3. Resolution floor: 4x (0.88 µm/px) accepted for now. 2x does not fit losslessly; it would need a lossy viewer format.
4. Can the native-resolution on-demand path read the external drive fast enough? To be measured.
5. Which viewer to use if Neuroglancer's UX proves unsuitable?

### 6.2 Questions for the supervisor (meeting, November 2026)

The owner takes these to the supervisor. Each question says what it decides and which default applies until it is answered. **Needed for** names the task that cannot be finished without the answer; all other questions only refine results.

**A. Sectioning**

| ID | Question | Decides | Until answered | Needed for |
|---|---|---|---|---|
| Q1 | How thick is each section (µm)? | Z proportions of the volume, 3D cell size, densities per mm³ | `z_spacing_um` 5 µm (placeholder); densities per mm² of section | correct proportions (M7-15, M7-17) |
| Q2 | Were all consecutive sections of the block mounted and scanned, or only every n-th one? If not all: how many µm lie between two scanned sections? *Our observation:* neighbouring images share vessels; after a rough local alignment only 10 to 17% of nuclei recur, and images two apart share none. With consecutive sections of 3 to 5 µm we would expect more, unless our alignment is still too coarse. | Whether a cell's profiles must be linked across sections (M7-12, M7-13) | Owner's information: all scanned, none left out | M7-12 |
| Q3 | Do the file numbers 1 to 140 follow the cutting order? Was any section lost, damaged, re-cut or put out of order? | Slice order; known gaps | Numbers are the cutting order; nothing missing | — |
| Q4 | In which anatomical plane and direction was the block cut, and where is the pial surface in the images? | Orientation labels; reading of cortical layers | No anatomical labels | — |
| Q5 | Paraffin (FFPE) or frozen sections? Is a shrinkage factor known? | Whether sizes in µm compare with literature values | No shrinkage correction | — |

**B. Stain**

| ID | Question | Decides | Until answered | Needed for |
|---|---|---|---|---|
| Q6 | Which stain exactly? It looks like hematoxylin without eosin: nuclei dark purple, tissue pale purple, red blood cells beige. Could it be a Nissl stain (cresyl violet)? Same protocol for all 140 slides? | Colour separation for detection; whether neuron bodies, not only nuclei, can be measured | Hematoxylin only | M7-01 conclusions |
| Q7 | Are other stains of this block or of neighbouring blocks available (for example NeuN, GFAP, Olig2, Iba1, vimentin, SMI32, CD34)? Could a few sections be stained and scanned? | Whether cell types can be verified beyond morphology | Types from morphology only | — |

**C. Cells**

| ID | Question | Decides | Until answered | Needed for |
|---|---|---|---|---|
| Q8 | Which FCD type does this case have (pathology report)? | Which abnormal cells to expect (dysmorphic neurons, balloon cells) | Unknown | M9 |
| Q9 | Which cell types should the first version distinguish? Proposal: neuron, glia, vessel-wall cell, unclassified. Is it worth separating astrocytes, oligodendrocytes and microglia in this stain? | Type table; labelling guide | The proposal | M7-00, M7-02 |
| Q10 | Should a cell be represented by its nucleus (clearly visible) or by its whole cell body (faint in this stain)? | What is measured; blob size | Nucleus | M7-00 |
| Q11 | Typical nucleus (and cell body) diameters per type in this tissue, normal and abnormal? | Size limits for detection; plausibility checks | 3 to 15 µm nuclei | M7-05 |
| Q12 | What makes a cell suspicious in this case? For example: nucleus or body larger than a given size, prominent nucleolus, abnormal shape or orientation, position (neurons in white matter), clusters. | Which measurements are stored now, so that M9 needs no second detection | A broad set of measurements | M7-05 |
| Q13 | Is the lesion location known (report, MRI, a rough outline on one slide)? | Validation of density maps and of the suspicious-cell search | Unknown | M7-15, M9 |

**D. Vessels and other structures (M8)**

| ID | Question | Decides | Until answered | Needed for |
|---|---|---|---|---|
| Q14 | Which vessels matter (capillaries, arterioles, veins) and from which diameter on? Should arteries and veins be told apart? | Vessel detection | All vessels with a visible lumen | M8 |
| Q15 | Which other structures should be visible in 3D: cortical layers, grey/white-matter boundary, pial surface, lesion outline, calcifications, heterotopic neurons? | Scope of M8 | None | M8 |

**E. Labelled examples and accuracy**

| ID | Question | Decides | Until answered | Needed for |
|---|---|---|---|---|
| Q16 | Who can mark cells on example crops for training and testing (supervisor, a pathologist, the owner), and how much time is available? Plan: 15 to 20 crops of 250 × 250 µm, about 1500 cells, a few hours. | Ground truth for the accuracy gates | — | M7-02, M7-10, M7-11 |
| Q17 | Is QuPath used at the institute? Do annotations or QuPath projects for these slides exist? | Labelling tool; export format | QuPath | M7-02 |
| Q18 | Is a detection score (F1) of at least 0.90 and a median size error of at most 15% good enough? For suspicious cells: rather find all candidates (more false alarms to review) or few false alarms? | Gates of AC10; thresholds in M9 | The proposal; all candidates | M7-10 |

**F. Software, computers, data**

| ID | Question | Decides | Until answered | Needed for |
|---|---|---|---|---|
| Q19 | May deep-learning nucleus detectors be installed (PyTorch with Cellpose, StarDist or InstanSeg; open source, about 1 to 2 GB) and scikit-learn? Any licence restrictions? | Detector and classifier | Baseline detector only | M7-01 comparison, M7-11 |
| Q20 | Is a workstation or server with a GPU available? | Run time for 140 slices (laptop estimate: 1 to 4 days for detection) | The laptop | M6-03 |
| Q21 | Data protection: may derived data (cell tables, result figures showing tissue) be kept in the private GitHub repository or cloud services? Compressed result figures are already there. | What may go to GitHub | Figures only, as now | — |
| Q22 | The 140-slice volume needs about 280 GB of the about 423 GB free on the work drive. Is a second drive available for cells, density maps and later versions? | Scale-up | One drive | M6-03 |

**G. Scope**

| ID | Question | Decides | Until answered | Needed for |
|---|---|---|---|---|
| Q23 | Priority: cells in 3D on slices 1 to 10 first (current plan), or the whole 140-slice volume first? | Order of M7 and the scale-up | Cells first | — |
| Q24 | Who will use the result (pathologists, presentations, a publication)? Must the viewer run on other computers? | Viewer hosting; export formats | Local viewer | M6-03 |
| Q25 | Which deadlines apply (presentations, reports)? | Order and scope | — | — |

**H. For the owner** (no supervisor needed)

| ID | Question | Decides | Until answered | Needed for |
|---|---|---|---|---|
| Q26 | Blob shapes: a sphere per cell, colour per type, optionally a fixed proportion per type (flatter or taller). Truly different shapes need meshes (more work, slower). Is that what "different shapes" should mean? | Layer format (C17) | Spheres, colour per type | M7-00 |
| Q27 | Colours per type, or a convention used at the institute? | Type table | A colour-blind-safe palette | M7-00 |
| Q28 | (Lab) Were sections lost or discarded between slides 3/4, 4/5, 5/6 and 7/8? After cell-scale alignment these pairs share no nuclei, while 1→2, 2→3, 6→7, 8→9 and 9→10 share 9 to 20% (M7-12). How many sections are missing per gap? | z positions in the volume (non-uniform spacing), linking | Owner (2026-11-09): assume no gaps until the lab answers | M7-13, M7-17 |

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
  - **Undamaged areas:** the inlier residual is about 17 µm, roughly 2.5 registration pixels or 20 base pixels at 0.88 µm/px. That is structure-level alignment: vessels and layers line up, individual cells do not. (Whether single nuclei recur in the next section was measured later, see below.)
  - **Damaged areas:** at folds, tears and compressed margins (slices 4 and 5), edges stay off by hundreds of µm after any global transform. The best mask overlap after guided matching is 0.84 for pairs 4→5 and 5→6, against 0.93 to 0.98 for undamaged pairs. Fixing this needs non-rigid registration (post-MVP).

### QC calibration (M4-05, 2026-11-07, read only, slices 1 to 10)

How much do the QC scores drop when a registered pair is deliberately misaligned? Each pair's transform was shifted (25 to 400 µm, 4 directions) or rotated about the tissue centre (0.5° to 5°, both ways) and scored like QC (centre-weighted NCC, Dice). Script in the session scratchpad; numbers in `results/M4-05_qc_review/calibration.json`.

| Pair | NCC registered | shifted 200 µm | shifted 400 µm | rotated 5° | Dice registered | Dice shifted 400 µm | Dice rotated 5° |
|---|---|---|---|---|---|---|---|
| 1→2 | 0.475 | 0.413 | 0.346 | 0.307 | 0.981 | 0.963 | 0.940 |
| 2→3 | 0.536 | 0.457 | 0.364 | 0.342 | 0.964 | 0.950 | 0.933 |
| 3→4 | 0.451 | 0.418 | 0.363 | 0.357 | 0.944 | 0.934 | 0.922 |
| 4→5 | 0.346 | 0.312 | 0.270 | 0.270 | 0.904 | 0.896 | 0.887 |
| 5→6 | 0.377 | 0.348 | 0.281 | 0.294 | 0.919 | 0.913 | 0.908 |
| 6→7 | 0.724 | 0.530 | 0.411 | 0.394 | 0.977 | 0.951 | 0.924 |
| 7→8 | 0.682 | 0.549 | 0.431 | 0.410 | 0.976 | 0.953 | 0.926 |
| 8→9 | 0.754 | 0.551 | 0.430 | 0.399 | 0.981 | 0.959 | 0.935 |
| 9→10 | 0.714 | 0.544 | 0.423 | 0.392 | 0.980 | 0.960 | 0.936 |

(Means over the directions; maxima in the JSON.)

- **NCC falls smoothly with the error, but its level differs between pairs** (0.35 to 0.75), more than a 200 µm error changes it. A correctly aligned damaged pair (4→5: 0.346) scores like a good pair 400 µm off (1→2: 0.346). An absolute threshold therefore finds damaged pairs and large errors, not errors of 100 to 200 µm.
- **Dice hardly reacts to misalignment** (−0.01 to −0.03 at 400 µm); it measures torn, folded or missing margins, and rotations of about 5°.
- **Damaged pairs have a flat optimum:** for 3→4, 4→5 and 5→6 a shift of 100 µm or a rotation of 1° in one direction scores up to 0.012 higher than the feature-based result. For the undamaged pairs the registered transform is the clear maximum.
- **Chosen (approved by the owner for now, 2026-11-07):** `qc_min_ncc` 0.40 and `qc_min_dice` 0.93. Both lie in the gap between the damaged pairs 4→5, 5→6 (flagged) and the rest. Together they catch a 5° rotation in every pair, a 400 µm shift in pairs 1 to 5, and the gross failures seen so far (the −12.7° drift of pair 4→5 in M3-08 had NCC 0.246).
- **Visual check of the 9 overlays:** interiors (ventricles, central boundary, curved band) are grey in every pair; the coloured areas are torn, folded or missing tissue at the margins, largest in 4→5 and 5→6.

### Same cells in neighbouring sections? (2026-11-05, read only, slices 1 to 10)

Question: does a cell continue from one image into the next? Script in the session scratchpad (not in the repository), outputs only there; sources read at the 2x level (0.44 µm/px).

- **Method:**
  - 300 µm windows of section *a*, more than 1.5 mm inside the tissue, three windows per pair;
  - section *b* warped into *a*'s frame with the registration transforms, then a local search of ±80 µm by correlation of the hematoxylin channel;
  - nuclei found by a simple threshold; a nucleus counts as shared if *b* has one within 2 µm;
  - control: the same comparison 1.5 mm away (chance level).
- **Results:**

  | Pair | Correlation (chance) | Shared nuclei (chance) |
  |---|---|---|
  | 1→2 | 0.07 to 0.11 (0.04 to 0.05) | 2 to 7% (2 to 6%); the local search did not lock on |
  | 6→7 | 0.14 to 0.33 (about 0.05) | 10 to 17% (2 to 3%) |
  | 8→9 | 0.09 to 0.24 (about 0.05) | 8 to 15% (4 to 5%) |
  | 1→3, 6→8 (two apart) | 0.04 to 0.08 | chance level |

- **Visual check (6→7):** vessels and larger structures lie in the same places; most single nuclei are not recognisably the same.
- **Local offsets after the registration:** 20 to 25 µm where the search clearly worked, once almost 80 µm. These are 2 to 4 cell diameters, so following single cells across sections needs a local alignment at cell scale (M7-12).
- **Interpretation:** neighbouring images are close in depth. The low share of recurring nuclei fits either gaps between scanned sections or adjacent thin sections with local alignment errors of a few µm (one rigid offset per 300 µm window cannot follow the deformation inside it). The owner reports that all sections were scanned (question Q2). The spike M7-01 repeats the measurement with a finer local alignment.
- **Stain and size (sample slide, small crops):**
  - looks like hematoxylin only: nuclei dark purple, tissue pale purple, red blood cells beige, no eosin (question Q6);
  - nuclei are sharp at 0.22 to 0.44 µm/px with no blurred nuclei below them, so the sections are thin;
  - median nucleus diameter about 6 µm (90th percentile 7 to 8.5 µm);
  - roughly 500 to 1000 nuclei/mm² from a simple threshold (probably an undercount), so about 200,000 to 400,000 nucleus profiles per section.
- **Viewer:** the bundled Neuroglancer (2.41.2) supports ellipsoid annotations with per-cell properties, controls in the layer panel and sharded annotation files. Its Python annotation writer is too limited (one file per annotation, no multi-level spatial index), so the cell layers need an own writer (numpy only).

### Cell spike (M7-01, 2026-11-07, read only, `scripts/spike_cells.py`)

Outputs in the work dir `spike/M7-01/` (figures, `detect.json`, `recur.json`, test cell layers). Sources were only read (levels 0 and 1, crops; the 32x level for masks of slices 70 and 140).

**Detectors** (installed with the owner's permission, 2026-11-07: PyTorch 2.15 with Cellpose 4.2.1 and InstanSeg 0.1.2; TensorFlow 2.21 with StarDist 0.9.2). 12 crops of 250 × 250 µm, at least 0.4 mm inside the tissue, 3 each from slices 1, 5, 70 and 140.

| Detector | Seconds per mm² of tissue (1x / 2x) | Per slice of 400 mm², 2x | Behaviour |
|---|---|---|---|
| Baseline (colour separation, threshold, watershed) | 2.0 / 0.6 | about 4 min | Fragments nuclei and texture: up to 700 objects where the others find 50 to 70. Not usable |
| StarDist `2D_versatile_he` | 3.9 / 4.0 | about 27 min | Finds a large pale neuron nucleus the others miss (slice 1); misses many nuclei in a pale, soft crop (slice 5) |
| InstanSeg `brightfield_nuclei` | 1.6 / 1.0 | about 7 min | Finds most nuclei in the pale crop, including large pale ones; missed the pale neuron nucleus of slice 1 |
| Cellpose 4 (`cpsam`, Apple GPU) | 135 / 34 | about 4 h (15 h at 1x) | Misses most nuclei in the pale crop; too slow for 140 slices on this laptop |

- **Agreement between the three deep-learning detectors:** in sharp crops, 0.80 to 0.95 (F1 of one-to-one matching within 3 µm); in the 3 blurred crops (focus score 11 to 56 against 150 to 280), 0.43 to 0.76.
- **Resolution:** the same detector at 2x (0.44 µm/px) against 1x (0.22) agrees 0.78 to 0.98. 2x is enough for finding nuclei, and has 4 times fewer pixels to read.
- **Size:** median nucleus diameter 6.1 to 7.6 µm per crop, consistent between detectors (equivalent-area circle).
- **Accuracy itself is not known yet:** agreement is not correctness. Pale neuron nuclei, the cells that matter most for FCD, are the weak spot of every detector. This needs the labelled crops (M7-02) and probably fine-tuning on them.
- **Nucleolus:** visible as a dark dot in sharp nuclei at 1x (gallery of the 48 largest nuclei). At 2x a nucleolus is only 2 to 4 pixels, so the nucleolus flag needs 1x patches around the larger nuclei.
- **Focus varies within slices:** 3 of 12 crops (slices 5 and 140) are clearly out of focus. Detection there is unreliable, so a sharpness score per tile is needed (M7-16).
- **Stain:** some neurons show a stained cell body around a pale nucleus with a dark nucleolus. This looks like a Nissl-type stain rather than hematoxylin only, which makes question Q6 more important.
- **Block:** slice 140 has only 67 mm² of tissue (slice 70: 283 mm², slice 1: 397 mm²).

**Recurrence of nuclei in the next section** (pairs 1→2, 6→7, 8→9, and 69→70 registered in memory: `features`, 391 inliers). For each pair, 4 sharp windows of 150 µm at 1x; the next section was warped by the registration, aligned locally by correlation (±100 µm), nuclei were detected (InstanSeg) and fitted to each other. The control is the same procedure 1.5 mm away.

- **Shared nuclei within 1.5 µm, per window:**
  - 1→2: 5 to 14%;
  - 6→7: 0 to 16%;
  - 8→9: 11 to 33%, up to 49% within 2.5 µm;
  - 69→70: 0 to 12%;
  - controls: 0 to 12%.
- **The windows that locked on best** (correlation 0.32 to 0.38, all in 8→9) have the most shared nuclei; windows with correlation 0.1 to 0.2 stay near chance, so their local alignment is most likely wrong. Fitting the nuclei to each other did not improve this.
- **Model** (spheres 6.5 µm, profiles of radius ≥ 1.6 µm counted):
  - adjacent sections 3 to 10 µm thick: 36 to 66% of profiles recur;
  - gaps of 10 µm or more: none;
  - so the measured values above the control **support adjacent sections** (owner, Q2), with detection misses and leftover deformation lowering them.
- **Conclusion:** cells continue into the next section, so linking (M7-12, M7-13) is needed. The local alignment must be stronger than a correlation per window (smooth field, larger context, robust fit); this is the main risk of M7.

**Viewer load test:**
- **Test cells:** 4.19 million in 3 test types (70 / 20 / 10%), at 1000 per mm² inside the tissue masks of slices 1 to 10, placed through the chained transforms in the volume's frame.
- **Format:** the prototype writer of contract C17 (ellipsoids in µm, 6-level sharded spatial index, sharded by-id index).
- **Cost:** 13 s to write, 426 MB, 8 files per layer. A test read of the by-id index returns the right cell.
- **Owner check in the browser (2026-11-07):** works without problems: smooth in 2D and 3D, layers toggle and recolour, the confidence slider filters, a clicked cell shows its properties, and the tissue fades to opacity 0.

### Appearance groups (M7-18, 2026-11-08, read only, `scripts/appearance_groups.py`)

Outputs in the work dir `spike/M7-18/` (`group_<g>.jpg`, `overview.jpg`, `groups.json`, `cells.csv`).

- **Cells:** 2795 nuclei (InstanSeg, level 0) from 40 crops of 250 µm: the 20 label crops and 20 more on slices 8 to 135.
- **Features (9):** diameter, elongation, solidity, darkness, chromatin texture, nucleolus contrast, cell-body stain around the nucleus, halo brightness, distance to a vessel lumen. They were standardised; diameter and elongation on a log scale.
- **Grouping:** k-means for 3 to 10 groups.
  - **Stability** (agreement over 15 re-runs on 80% of the cells): 0.77 (3), 0.87 (4), 0.86 (5), 0.72 (6), 0.91 (7), **0.92 (8)**, 0.79 (9), 0.70 (10). Eight groups were used.
  - **Separation** (silhouette): only 0.13 to 0.16 for every number of groups. The looks form a continuum with dense regions, not sharply separated types. A classifier trained on labels is needed; the groups only map the variety.
- **Groups** (by median diameter) and what the galleries show:

  | Group | Share | Median looks | Matches |
  |---|---|---|---|
  | 0 | 14% | 6.0 µm, round, darkest | Oligodendrocyte-like |
  | 1 | 23% | 6.2 µm, round, medium dark | Between oligodendrocyte-like and astrocyte-like |
  | 2 | 10% | 6.3 µm, elongated (1.96) | Microglia-like or vessel-wall |
  | 3 | 17% | 6.6 µm, round, dark, **clear halo** | Oligodendrocyte-like (clearest group) |
  | 4 | 3% | 6.9 µm, very elongated or irregular (2.28) | Microglia-like or vessel-wall, plus detector errors (outlines including part of a cell body) |
  | 5 | 8% | 7.4 µm, nucleus inside a **dark stained cell body** | Neuron |
  | 6 | 12% | 7.5 µm, **palest**, little texture | Astrocyte-like (plus faint nuclei) |
  | 7 | 13% | 8.8 µm, round, textured, nucleolus | Neuron (plus some large astrocyte-like) |

- **Conclusion:** the data supports the six-class list of decision 0006, and no group falls outside it. Confirmed by the owner (2026-11-08), together with the clarifications in `docs/LABELLING.md`.
  - Neurons appear in two looks: a large nucleus with a nucleolus, and a nucleus inside a stained cell body.
  - Microglia-like and vessel-wall cells look alike as nuclei. They differ by their surroundings, and the vessel feature failed (lumens are rarely open in this tissue), so labels must separate them.
  - Group 1 marks where oligodendrocyte-like and astrocyte-like meet. The labels matter most there.
- **Feature limits:**
  - the cell-body feature is near zero for most cells and the halo feature carries it instead;
  - the vessel distance is capped for nearly all cells.

  Both were changed in M7-05d (cell body: upper quartile of the ring; lumens: 0.85 × 255 and 15 µm², distance capped at 20 µm); whether they now separate the classes is measured with the expert labels (M7-11).

### Nucleus detection on real slices (M7-05, 2026-11-08, read only)

1 mm squares around the label crops of slices 1, 60 and 140 (6 mm² of tissue), InstanSeg at 2x through `detect_slice`:
- **Counts:** 1100 to 1730 nuclei per mm²; median radius 3.1 to 3.3 µm.
- **Against the draft points of M7-19 (not ground truth):**
  - 90 to 95% of the detections are draft points, at a median distance of 0.2 to 0.3 µm;
  - 77 to 97% of the draft points are found (the drafts also hold StarDist and level-0 detections).
- **Nucleolus:** a strict rule, calibrated by eye. It flags 4 to 15% of the nuclei of 8 µm or more, and the examples shown are mostly real nucleoli. Many large nuclei here have coarse chromatin clumps that look like nucleoli at 2x.
- **Sharpness:** the level-0 value is 0.18 × the 2x value (60 windows). Around one crop of slice 60, 9 of 21 tiles fall below the limit of 100.
- **Speed:** about 2.5 s per mm² expected on whole slices (6 to 8 s in the small test regions).

### Detection of a whole slice (M7-06, 2026-11-08, `brainrec detect --slices 1`)

- **Slice 1:** 436,334 nuclei on 397 mm² (1099 per mm²); 990 s (2.5 s per mm²); cell file 48 MB; peak memory 4.1 GB.
- **Classes (interim classifier rules-v1, not checked against labels):** Neuron 19%, Astrocyte-like 34%, Oligodendrocyte-like 34%, Microglia-like 7%, Vessel-wall 6%.
- **Focus:** 26% of the tiles (21% of the nuclei) are below the sharpness limit. They form compact patches of lost focus in the scan.
- **For all 140 slices:** about 39 h of detection and about 7 GB of cell files.

### Slices 1 to 10 and their detection QC (M7-09, M7-16, 2026-11-09)

- **Detection, slices 1 to 10:** 4.55 million nuclei; 16 to 29 min per slice including reading (23 min on average, so about 54 h for 140 slices rather than 39 h); cell files 488 MB.
- **Density:** 1027 to 1147 profiles per mm² of section; each slice lies within 0.94 to 1.07 of its neighbours, and class shares within 0.83 to 1.10.
- **Focus:** 20 to 40% of the tiles are blurred (17 to 39% of the profiles).
- **Folds** (dark regions of at least 2× the median tissue density on the 32x level):
  - slices 1 to 5: 0.8 to 2.0% of the tissue, with 4,765 to 9,905 profiles inside folds;
  - slices 6 to 10: 0.0 to 0.2%.
- **Interim classifier inside folds:** many nuclei in folds are called Neuron because the tissue there stains darker.

### Cell-scale alignment of slices 1 to 10 (M7-12, 2026-11-09, `brainrec cells --slices 1-10`)

- **Method:** votes of nucleus offsets per 100 µm node (decision 0007). 6 s per pair; about 14 min for all 140 slices.
- **Adjacent pairs** (1→2, 2→3, 6→7, 8→9, 9→10):
  - 38 to 78% of nodes lock on;
  - local shifts after the registration, median 13 to 28 µm (up to about 70 µm);
  - 9 to 20% of all nuclei have a partner within 1.5 µm (chance 0.8%);
  - neighbouring nodes 100 µm apart agree within 0.3 to 0.6 µm.
- **Pairs without shared nuclei** (3→4, 4→5, 5→6, 7→8): chance level, also with a ±300 µm search and although 7→8 is well registered.
  - No non-consecutive pair among slices 1 to 10 shares nuclei, so the order is consistent.
  - Probably missing sections (Q28); the owner assumes none for now.
- **Expected for truly adjacent sections:** 36 to 66% recurrence for 3 to 10 µm sections (M7-01 model). The measured 9 to 20% are lower because of detection misses, blur and leftover deformation in unlocked regions.

### Linking into 3D cells, slices 1 to 10 (M7-13, 2026-11-09)

- **Result:** 4,554,350 profiles form 4,189,396 cells (8% fewer). 356,000 cells span 2 sections and 4,500 span 3; no chain exceeded 8 sections.
- **Links per aligned pair:** 10.5 to 21.3% of the profiles, against 0.8 to 0.9% at a wrong offset, so about 5% of the links may be false. Median link distance 0.76 µm.
- **Fewer links than expected:** a model of 3 to 10 µm sections expects 36 to 66% of nuclei to continue. Links are fewer mainly because of detection misses, blur and regions where the alignment does not lock. The cell count therefore still counts part of the nuclei twice. That has to be corrected statistically (Abercrombie) once the section thickness is known.

### Cell table and QuPath export (M7-14, 2026-11-09)

- **`cells/table.csv`:** 4,189,396 rows for slices 1 to 10, 915 MB (218 bytes per cell, so about 13 GB for 140 slices). That is more rows than Excel holds (1,048,576); it is meant for Python, R or QuPath.
- **QuPath export of slice 9:** 457,765 detections, 504 MB (1,100 bytes per detection), written in 16 s.
- **Outlines that are not simple:** 2.8% of the outlines of slice 9 fold over or touch themselves. That comes from thinning outlines to 32 vertices in detection (M7-05). QuPath rejects 0.06% of them, and with them the whole import. The export writes such outlines as their convex hull; a fix at the source (thinning without folds) would need detection to run again.
