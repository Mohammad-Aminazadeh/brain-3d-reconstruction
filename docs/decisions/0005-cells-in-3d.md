# 0005: Cells and structures as 3D objects

Date: 2026-11-06. Status: approved by the project owner (direction and ideas, 2026-11-05 and 2026-11-06). Details marked Proposed (types, shapes, thresholds, detector, contracts C13 to C18) are fixed in the spike (M7-01) and the contracts task (M7-00), partly after the supervisor's answers (`docs/REQUIREMENTS.md` section 6.2).

## Context

The owner changed the goal of the project (2026-11-05):
- **Goal:** see cells of different types, vessels and other structures in 3D.
- **Rest of the tissue:** everything else can be made transparent, so that only these structures remain visible inside the reconstructed volume.
- **Method (owner):**
  - detect the centre of each cell (cells look roughly circular);
  - replace it with a 3D blob of the same centre;
  - take the radius from the 2D circle;
  - give different types different shapes and colours.
- **Accuracy:** detection must be accurate enough to find suspicious cells later.

**Question raised by the owner:** can one cell be cut so that part of it lies in one image and part in the next? The answer decides whether one detection equals one cell.

**Evidence (2026-11-05, read only, slices 1 to 10, 2x level):**
- **Method:** 300 µm windows deep in the tissue, first aligned by the registration, then by a local search of ±80 µm.
- **Neighbouring images:**
  - vessels and larger structures recur;
  - only 10 to 17% of nuclei have a partner within 2 µm in the best windows (chance: 2 to 6%).
- **Images two apart:** nothing above chance.
- **Local offsets:** the local search had to move the next image by 20 to 25 µm, once almost 80 µm, beyond the registration.
- **Interpretation:** the data fits two explanations:
  - gaps between the scanned sections; or
  - adjacent thin sections whose local alignment is not yet fine enough (a few µm) to pair single nuclei.
- **Owner (2026-11-06):** as far as known, all sections were scanned and none was left out. Section thickness is unknown and will be supplied later.

**Physics:**
- The microtome cuts straight through cells. In consecutive sections a few µm thick, each nucleus (about 6 to 15 µm) and each neuron body (10 to 30 µm or more) appears in several consecutive images.
- A single image holds only a slice of a cell. Its 2D radius is at most the true radius, about 80% of it on average for random cuts.

## Decision

1. **Cells are objects, not pixels.**
   - Each nucleus is detected in its own section, in the slice frame, inside the tissue mask, at about 0.44 µm/px (2x).
   - It is stored with its centre, radius, outline, measurements (including a nucleolus flag), type and probabilities.
   - It is placed in 3D through the registration transforms.
   - The image volume is unchanged; the cells are separate viewer layers.
2. **One cell, one blob.**
   - Profiles of the same cell in consecutive sections are linked into one 3D cell. This needs a local alignment at cell scale (a few µm) between neighbouring sections, computed from the detected nuclei on top of the registration.
   - Size comes from the largest linked profile, the one closest to the cell's equator.
   - The blob sits on that profile, so it lies on its nucleus in the volume.
   - If the sections turn out to have gaps, linking simply finds no partner and each profile stays its own cell. The same code covers both cases.
3. **Representation.**
   - Neuroglancer ellipsoid annotations: one sharded layer per cell type, colour and proportions per type from a configured type table.
   - Default (Proposed): spheres, types differ by colour and optionally by a fixed proportion.
   - The tissue layer gets an opacity control down to fully transparent.
   - "Background" keeps its meaning: only the glass outside the tissue (C11).
4. **Accuracy is measured, not claimed.**
   - **Labelled crops:** owner or pathologist labels on 15 to 20 crops, with a held-out test set.
   - **Detectors:** a baseline detector without new dependencies is compared with pretrained deep-learning detectors (permission pending).
   - **Types:** a classifier on the measurements.
   - **Gates (Proposed):** detection F1 at least 0.90, median radius error at most 15%.
   - **Size:** judged on profiles with a visible nucleolus, which are cut close to the middle of the nucleus.
5. **Cell table as the main result.**
   - Every cell with its 3D position, type, size, measurements and position in the source slide, exported as CSV.
   - Per slide as QuPath detections (GeoJSON), for review on the original slide at full resolution.
6. **Density maps per type.**
   - Cells per volume on a coarse 3D grid (about 50 µm), shown as an extra viewer layer, also in 3D.
   - Until the section thickness is known, densities are given per mm² of section. Afterwards they are given per mm³: from linked cells, or with the Abercrombie correction where profiles are counted.
7. **Vessels and other structures follow in M8.** Vessels recur across sections, so they also serve as landmarks for a local, non-rigid refinement of the registration, for cells and later for the volume.
8. **Suspicious cells follow in M9,** using criteria from the supervisor, on top of the stored measurements.
9. **Section thickness stays open.**
   - **Config:** `section_thickness_um` (unknown, `null`) next to `z_spacing_um` (placeholder).
   - **Independent of thickness:** detection, local alignment and linking.
   - **Dependent on thickness:** z proportions, 3D size estimates and densities per mm³, which are recomputed once the value is set.
10. **Not done:**
    - no invented images or cells between sections, because that would fabricate tissue;
    - no separate mesh per cell, because it is heavy to render and coloured spheres carry the same information.

## Consequences

- **New components:**
  - `detect`: detection, measurement and classification per slice;
  - `cells`: local alignment, linking, 3D placement, layers, exports and density maps.
- **New stages and commands:** `detect` and `cells`, with the interim commands `brainrec detect` and `brainrec cells`.
- **New interfaces:** the contracts C13 to C18 and new config fields, fixed in M7-00.
- **`neuroglancer_state()`:** gets optional cell and density layers; existing calls keep working.
- **New milestone M7 "Cells in 3D"** before M5. The MVP now ends with linked, typed 3D cells of slices 1 to 10 and their cell table.
- **Later milestones:** M8 (vessels, structures, landmark-based non-rigid refinement) and M9 (suspicious cells) come after the MVP.
- **Unchanged done tasks:** all of them. Volume, transforms, masks and registration stay as they are.
- **Changed to-do tasks:** M5-01, M5-02, M5-03, M5-05, M5-06, M6-02 and M6-03 now include the new stages.
- **Run time:**
  - detection reads each slide once at 2x, roughly 5 to 40 min per slice depending on the detector;
  - that is roughly 1 to 7 h for 10 slices and 1 to 4 days for 140, resumable per slice;
  - the spike measures it.
- **Storage:** cell files under 10 GB for all 140 slices; QuPath exports (GeoJSON) are larger and are written for the requested slices. The spike and M7-09 measure both.
- **Dependencies:** possible new ones (PyTorch with a pretrained detector; scikit-learn) need the owner's permission before installation.
- **Open questions** for the supervisor: section thickness, stain, cell types, criteria for suspicious cells, labelling, software. They are listed in `docs/REQUIREMENTS.md` section 6.2. Until answered, the Proposed defaults apply.
